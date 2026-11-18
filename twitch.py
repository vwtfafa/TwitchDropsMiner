from __future__ import annotations

import os
import re
import json
import asyncio
import logging
from time import time
from copy import deepcopy
from itertools import chain
from functools import partial
from collections import abc, deque, OrderedDict
from datetime import datetime, timedelta, timezone
from contextlib import suppress, asynccontextmanager
from typing import Any, Literal, Final, NoReturn, overload, cast, TYPE_CHECKING

import aiohttp
from yarl import URL

from translate import _
from gui import GUIManager
from channel import Channel
from websocket import WebsocketPool
from inventory import DropsCampaign
from exceptions import (
    ExitRequest,
    GQLException,
    ReloadRequest,
    LoginException,
    MinerException,
    RequestInvalid,
    CaptchaRequired,
    RequestException,
)
from utils import (
    CHARS_HEX_LOWER,
    chunk,
    timestamp,
    create_nonce,
    task_wrapper,
    first_to_complete,
    RateLimiter,
    AwaitableValue,
    ExponentialBackoff,
    redact_url,
    redact_sensitive,
    find_browser_executable,
    backup_cookies,
)
from updater import (
    UpdateInfo,
    apply_windows_update,
    can_auto_apply,
    consume_update_success,
    download_update,
    fetch_update_info,
)
from constants import (
    CALL,
    MAX_INT,
    DUMP_PATH,
    COOKIES_PATH,
    MAX_CHANNELS,
    GQL_QUERIES,
    WATCH_INTERVAL,
    State,
    JsonType,
    ClientType,
    PriorityMode,
    WebsocketTopic,
    creator_badge_owned_campaigns_query,
)
from creator_badges import (
    CreatorBadgeEvent,
    active_creator_badge_event_for_channel,
    apply_creator_badge_progress,
    event_watch_rewards_complete,
    has_incomplete_watch_rewards,
    is_stuck,
    parse_creator_badge_event,
    remaining_watch_minutes,
    select_targets,
    should_mine_creator_badges,
)
from lifecycle import (
    DEFERRED_REQUESTS,
    SETTLED_STATES,
    STATE_STATUS,
    STATE_TRAY_ICON,
    URGENT_REQUESTS,
    create_miner_statemachine,
)
from statemachine import Transition

if TYPE_CHECKING:
    from utils import Game
    from gui import LoginForm
    from channel import Stream
    from settings import Settings
    from inventory import TimedDrop
    from constants import ClientInfo, GQLOperation


logger = logging.getLogger("TwitchDrops")
gql_logger = logging.getLogger("TwitchDrops.gql")


class SkipExtraJsonDecoder(json.JSONDecoder):
    def decode(self, s: str, *args):
        # skip whitespace check
        obj, end = self.raw_decode(s)
        return obj


def SAFE_LOADS(s: str) -> JsonType:
    return json.loads(s, cls=SkipExtraJsonDecoder)


class _AuthState:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._lock = asyncio.Lock()
        self._logged_in = asyncio.Event()
        self.user_id: int
        self.device_id: str
        self.session_id: str
        self.access_token: str
        self.client_version: str

    def _hasattrs(self, *attrs: str) -> bool:
        return all(hasattr(self, attr) for attr in attrs)

    @property
    def logged_in(self) -> bool:
        """True when a validated session (user ID + access token) is available."""
        return self._logged_in.is_set()

    def _delattrs(self, *attrs: str) -> None:
        for attr in attrs:
            if hasattr(self, attr):
                delattr(self, attr)

    def invalidate(self, *, delete_cookies: bool = False) -> None:
        self._delattrs("access_token", "user_id")
        self._logged_in.clear()
        self._twitch.gui.help._invalidate_button.config(state="disabled")
        if delete_cookies:
            session = self._twitch._session
            if session is not None:
                jar = cast(aiohttp.CookieJar, session.cookie_jar)
                jar.clear()
                backup_cookies()
                COOKIES_PATH.unlink(missing_ok=True)

    def clear(self) -> None:
        self._delattrs(
            "user_id",
            "device_id",
            "session_id",
            "access_token",
            "client_version",
        )
        self._logged_in.clear()
        self._twitch.gui.help._invalidate_button.config(state="disabled")

    async def _browser_login(self) -> str:
        import zendriver

        gui_print = self._twitch.gui.print
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type
        coro_unless_closed = self._twitch.gui.coro_unless_closed

        if client_info.CLIENT_ID != ClientType.ANDROID_APP.CLIENT_ID:
            # NOTE: The ANDROID_APP client uses "https://www.twitch.tv/" exactly
            # as the redirect URL (including the trailing slash).
            # Other clients may use different redirect URLs instead,
            # but this would have to be tested first. This below is a reminder to do that first.
            raise RuntimeError("Browser login has only been tested for the ANDROID_APP client")

        while True:
            gui_print(_("login", "browser", "startup"))
            # login_data = await login_form.ask_login()
            browser: zendriver.Browser | None = None
            try:
                backup_cookies()
                browser = await zendriver.start(
                    browser_executable_path=str(
                        find_browser_executable(self._twitch.settings.browser_path)
                    )
                )
                await browser.get(
                    f"https://www.twitch.tv/login?client_id={client_info.CLIENT_ID}&"
                    f"redirect_params=client_id%3D{client_info.CLIENT_ID}%26"
                    "redirect_uri%3Dhttps%253A%252F%252Fwww.twitch.tv%252F%26response_type%3Dtoken"
                )
                # auto login
                # username_input = await page.select("#login-username")
                # password_input = await page.select("#password-input")
                # await username_input.send_keys(login_data.username)
                # await password_input.send_keys(login_data.password)

                # login_button = await page.select('[data-a-target="passport-login-button"]')
                # await login_button.click()

                # token submit button css selectors
                # NOTE: These are old, the website could be using different selectors by now
                # Button: "screen="two_factor" target="submit_button"
                # Input: <input type="text" autocomplete="one-time-code" data-a-target="tw-input"
                # inputmode="numeric" pattern="[0-9]*" value="">

                # wait for the user to navigate away from the URL, indicating successful login;
                # alternatively, they can press on the login button again
                async def url_waiter(browser=browser):
                    while (
                        await browser.test_connection()
                        and browser.main_tab is not None
                        and not str(browser.main_tab.url).startswith(
                            "https://www.twitch.tv/#access_token="
                        )
                    ):
                        await asyncio.sleep(0.5)

                gui_print(_("login", "browser", "login_to_complete"))
                await first_to_complete([
                    url_waiter(),
                    coro_unless_closed(login_form.wait_for_login_press()),
                ])

                # check if the browser window is still open
                if await browser.test_connection() and browser.main_tab is not None:
                    match = re.search(r'#access_token=(\w+)', str(browser.main_tab.url), re.I)
                    if match is not None:
                        return match.group(1)
                    gui_print(_("login", "browser", "no_token"))
                else:
                    gui_print(_("login", "browser", "closed_window"))
            except FileNotFoundError:
                gui_print(_("login", "browser", "not_found"))
            finally:
                if browser is not None:
                    await browser.stop()
                    browser = None
            await coro_unless_closed(login_form.wait_for_login_press())

    async def _oauth_login(self) -> str:
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
            "Host": "id.twitch.tv",
            "Origin": str(client_info.CLIENT_URL),
            "Pragma": "no-cache",
            "Referer": str(client_info.CLIENT_URL),
            "User-Agent": client_info.USER_AGENT,
            "X-Device-Id": self.device_id,
        }
        payload = {
            "client_id": client_info.CLIENT_ID,
            "scopes": "",  # no scopes needed
        }
        while True:
            try:
                now = datetime.now(timezone.utc)
                async with self._twitch.request(
                    "POST", "https://id.twitch.tv/oauth2/device", headers=headers, data=payload
                ) as response:
                    # {
                    #     "device_code": "40 chars [A-Za-z0-9]",
                    #     "expires_in": 1800,
                    #     "interval": 5,
                    #     "user_code": "8 chars [A-Z]",
                    #     "verification_uri": "https://www.twitch.tv/activate?device-code=ABCDEFGH"
                    # }
                    response_json: JsonType = await response.json()
                    device_code: str = response_json["device_code"]
                    user_code: str = response_json["user_code"]
                    interval: int = response_json["interval"]
                    verification_uri: URL = URL(response_json["verification_uri"])
                    expires_at = now + timedelta(seconds=response_json["expires_in"])

                # Print the code to the user, open them the activate page so they can type it in
                await login_form.ask_enter_code(verification_uri, user_code)

                payload = {
                    "client_id": client_info.CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }
                while True:
                    # sleep first, not like the user is gonna enter the code *that* fast
                    await asyncio.sleep(interval)
                    async with self._twitch.request(
                        "POST",
                        "https://id.twitch.tv/oauth2/token",
                        headers=headers,
                        data=payload,
                        invalidate_after=expires_at,
                    ) as response:
                        # 200 means success, 400 means the user haven't entered the code yet
                        if response.status != 200:
                            continue
                        response_json = await response.json()
                        # {
                        #     "access_token": "40 chars [A-Za-z0-9]",
                        #     "refresh_token": "40 chars [A-Za-z0-9]",
                        #     "scope": [...],
                        #     "token_type": "bearer"
                        # }
                        self.access_token = cast(str, response_json["access_token"])
                        return self.access_token
            except RequestInvalid:
                # the device_code has expired, request a new code
                continue

    async def _login(self) -> str:
        logger.info("Login flow started")
        gui_print = self._twitch.gui.print
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type

        token_kind: str = ''
        use_chrome: bool = False
        payload: JsonType = {
            # username and password are added later
            # "username": str,
            # "password": str,
            # client ID to-be associated with the access token
            "client_id": client_info.CLIENT_ID,
            "undelete_user": False,  # purpose unknown
            "remember_me": True,  # persist the session via the cookie
            # "authy_token": str,  # 2FA token
            # "twitchguard_code": str,  # email code
            # "captcha": str,  # self-fed captcha
            # 'force_twitchguard': False,  # force email code confirmation
        }

        while True:
            login_data = await login_form.ask_login()
            payload["username"] = login_data.username
            payload["password"] = login_data.password
            # reinstate the 2FA token, if present
            payload.pop("authy_token", None)
            payload.pop("twitchguard_code", None)
            if login_data.token:
                # if there's no token kind set yet, and the user has entered a token,
                # we can immediately assume it's an authenticator token and not an email one
                if not token_kind:
                    token_kind = "authy"
                if token_kind == "authy":
                    payload["authy_token"] = login_data.token
                elif token_kind == "email":
                    payload["twitchguard_code"] = login_data.token

            # use fancy headers to mimic the twitch android app
            headers = {
                "Accept": "application/vnd.twitchtv.v3+json",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-US",
                "Client-Id": client_info.CLIENT_ID,
                "Content-Type": "application/json; charset=UTF-8",
                "Host": "passport.twitch.tv",
                "User-Agent": client_info.USER_AGENT,
                "X-Device-Id": self.device_id,
                # "X-Device-Id": ''.join(random.choices('0123456789abcdef', k=32)),
            }
            async with self._twitch.request(
                "POST", "https://passport.twitch.tv/login", headers=headers, json=payload
            ) as response:
                login_response: JsonType = await response.json(loads=SAFE_LOADS)

            # Feed this back in to avoid running into CAPTCHA if possible
            if "captcha_proof" in login_response:
                payload["captcha"] = {"proof": login_response["captcha_proof"]}

            # Error handling
            if "error_code" in login_response:
                error_code: int = login_response["error_code"]
                logger.info(f"Login error code: {error_code}")
                if error_code == 1000:
                    logger.info("1000: CAPTCHA is required")
                    use_chrome = True
                    break
                elif error_code in (2004, 3001):
                    logger.info("3001: Login failed due to incorrect username or password")
                    gui_print(_("login", "incorrect_login_pass"))
                    if error_code == 2004:
                        # invalid username
                        login_form.clear(login=True)
                    login_form.clear(password=True)
                    continue
                elif error_code in (
                    3012,  # Invalid authy token
                    3023,  # Invalid email code
                ):
                    logger.info("3012/23: Login failed due to incorrect 2FA code")
                    if error_code == 3023:
                        token_kind = "email"
                        gui_print(_("login", "incorrect_email_code"))
                    else:
                        token_kind = "authy"
                        gui_print(_("login", "incorrect_twofa_code"))
                    login_form.clear(token=True)
                    continue
                elif error_code in (
                    3011,  # Authy token needed
                    3022,  # Email code needed
                ):
                    # 2FA handling
                    logger.info("3011/22: 2FA token required")
                    # user didn't provide a token, so ask them for it
                    if error_code == 3022:
                        token_kind = "email"
                        gui_print(_("login", "email_code_required"))
                    else:
                        token_kind = "authy"
                        gui_print(_("login", "twofa_code_required"))
                    continue
                elif error_code >= 5000:
                    # Special errors, usually from Twitch telling the user to "go away"
                    # We print the code out to inform the user, and just use chrome flow instead
                    # {
                    #     "error_code":5023,
                    #     "error":"Please update your app to continue",
                    #     "error_description":"client is not supported for this feature"
                    # }
                    # {
                    #     "error_code":5027,
                    #     "error":"Please update your app to continue",
                    #     "error_description":"client blocked from this operation"
                    # }
                    gui_print(_("login", "error_code").format(error_code=error_code))
                    logger.info(str(login_response))
                    use_chrome = True
                    break
                else:
                    ext_msg = str(login_response)
                    logger.info(ext_msg)
                    raise LoginException(ext_msg)
            # Success handling
            if "access_token" in login_response:
                self.access_token = cast(str, login_response["access_token"])
                logger.info("Access token granted")
                login_form.clear()
                break

        if use_chrome:
            # await self._chrome_login()
            raise CaptchaRequired()

        if hasattr(self, "access_token"):
            return self.access_token
        raise LoginException("Login flow finished without setting the access token")

    def headers(self, *, user_agent: str = '', gql: bool = False) -> JsonType:
        client_info: ClientInfo = self._twitch._client_type
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
        }
        if user_agent:
            headers["User-Agent"] = user_agent
        if hasattr(self, "session_id"):
            headers["Client-Session-Id"] = self.session_id
        # if hasattr(self, "client_version"):
        #     headers["Client-Version"] = self.client_version
        if hasattr(self, "device_id"):
            headers["X-Device-Id"] = self.device_id
        if gql:
            headers["Origin"] = str(client_info.CLIENT_URL)
            headers["Referer"] = str(client_info.CLIENT_URL)
            headers["Authorization"] = f"OAuth {self.access_token}"
        return headers

    async def validate(self):
        async with self._lock:
            await self._validate()

    async def _validate(self):
        client_info: ClientInfo = self._twitch._client_type
        if not hasattr(self, "session_id"):
            self.session_id = create_nonce(CHARS_HEX_LOWER, 16)
        if not self._hasattrs("device_id", "access_token", "user_id"):
            session = await self._twitch.get_session()
            jar = cast(aiohttp.CookieJar, session.cookie_jar)
        if not self._hasattrs("device_id"):
            async with self._twitch.request(
                "GET", client_info.CLIENT_URL, headers=self.headers()
            ) as response:
                page_html = await response.text("utf8")
                assert page_html is not None
            #     match = re.search(r'twilightBuildID="([-a-z0-9]+)"', page_html)
            # if match is None:
            #     raise MinerException("Unable to extract client_version")
            # self.client_version = match.group(1)
            # doing the request ends up setting the "unique_id" value in the cookie
            cookie = jar.filter_cookies(client_info.CLIENT_URL)
            self.device_id = cookie["unique_id"].value
        if not self._hasattrs("access_token", "user_id"):
            # looks like we're missing something
            login_form: LoginForm = self._twitch.gui.login
            logger.info("Checking login")
            login_form.update(_("gui", "login", "logging_in"), None)
            for client_mismatch_attempt in range(2):
                for invalid_token_attempt in range(2):
                    cookie = jar.filter_cookies(client_info.CLIENT_URL)
                    if "auth-token" not in cookie:
                        self.access_token = await self._browser_login()
                        cookie["auth-token"] = self.access_token
                    elif not hasattr(self, "access_token"):
                        logger.info("Restoring session from cookie")
                        self.access_token = cookie["auth-token"].value
                    # validate the auth token, by obtaining user_id
                    async with self._twitch.request(
                        "GET",
                        "https://id.twitch.tv/oauth2/validate",
                        headers={"Authorization": f"OAuth {self.access_token}"}
                    ) as response:
                        if response.status == 401:
                            # the access token we have is invalid - clear the cookie and reauth
                            logger.info("Restored session is invalid")
                            assert client_info.CLIENT_URL.host is not None
                            jar.clear_domain(client_info.CLIENT_URL.host)
                            continue
                        elif response.status == 200:
                            validate_response = await response.json()
                            break
                else:
                    raise RuntimeError("Login verification failure (step #2)")
                # ensure the cookie's client ID matches the currently selected client
                if validate_response["client_id"] == client_info.CLIENT_ID:
                    break
                # otherwise, we need to delete the entire cookie file and clear the jar
                logger.info("Cookie client ID mismatch")
                jar.clear()
                backup_cookies()
                COOKIES_PATH.unlink(missing_ok=True)
            else:
                raise RuntimeError("Login verification failure (step #1)")
            self.user_id = int(validate_response["user_id"])
            cookie["persistent"] = str(self.user_id)
            logger.info(f"Login successful, user ID: {self.user_id}")
            login_form.update(_("gui", "login", "logged_in"), self.user_id)
            # update our cookie and save it
            jar.update_cookies(cookie, client_info.CLIENT_URL)
            jar.save(COOKIES_PATH)
        self._twitch.gui.help._invalidate_button.config(state="normal")
        self._logged_in.set()


class Twitch:
    # Higher wins when two external requests overlap. INVENTORY_FETCH subsumes
    # local rule/channel work; a lower-priority request arriving later is ignored.
    REQUEST_PRIORITY = {
        State.RESTART: 5,
        State.INVENTORY_FETCH: 4,
        State.RULES_UPDATE: 3,
        State.CHANNELS_CLEANUP: 2,
        State.CHANNEL_SWITCH: 1,
    }

    def __init__(self, settings: Settings, *, gui: GUIManager | None = None):
        self.settings: Settings = settings
        # State management
        # NOTE: `_state_change` is a wake-up signal for the main loop; the actual
        # state and its history live in the explicit state machine. This split is
        # what allows the loop to wait for either a new request or a pipeline step.
        self._lifecycle = create_miner_statemachine()
        self._state_change = asyncio.Event()
        # Externally requested states (buttons, websocket events, timers) are
        # latched here instead of overwriting an in-flight pipeline step.
        self._pending_state: State | None = None
        self._pending_reason: str = ""
        self.wanted_games: list[Game] = []
        self.inventory: list[DropsCampaign] = []
        self._drops: dict[str, TimedDrop] = {}
        self._campaigns: dict[str, DropsCampaign] = {}
        self._unlinked_warn_ids: set[str] = set()
        self._mnt_triggers: deque[datetime] = deque()
        # triggers at which a drop stops being completable in time, these re-pick the games
        self._deadline_triggers: set[datetime] = set()
        # Tray controls: pause mining / skip channel / defer current game for this session
        self._mining_paused: bool = False
        self._skipped_channel_ids: set[int] = set()
        self._deferred_game_names: set[str] = set()
        # Cached CampaignDetails payloads keyed by campaign id + a summary signature.
        # Unchanged campaigns skip the heavy per-campaign GQL round-trips on reload.
        self._campaign_detail_cache: dict[str, JsonType] = {}
        self._campaign_detail_sigs: dict[str, str] = {}
        # NOTE: GQL is pretty volatile and breaks everything if one runs into their rate limit.
        # Do not modify the default, safe values.
        self._qgl_limiter = RateLimiter(capacity=5, window=1)
        # Client type, session and auth
        self._client_type: ClientInfo = ClientType.ANDROID_APP
        self._session: aiohttp.ClientSession | None = None
        self._auth_state: _AuthState = _AuthState(self)
        # GUI
        self.gui = gui if gui is not None else GUIManager(self)
        # Storing and watching channels
        self.channels: OrderedDict[int, Channel] = OrderedDict()
        self.watching_channel: AwaitableValue[Channel] = AwaitableValue()
        self._watching_task: asyncio.Task[None] | None = None
        self._watching_restart = asyncio.Event()
        # Websocket
        self.websocket = WebsocketPool(self)
        # Maintenance task
        self._mnt_task: asyncio.Task[None] | None = None
        # When the hourly inventory refresh is scheduled to run. Shown in the
        # status hint while idle, so users don't have to guess when new campaigns
        # will appear.
        self._next_reload_at: datetime | None = None
        # Creator Badge watch-time targets discovered from follows (Task 4+).
        self._creator_badge_events: list[CreatorBadgeEvent] = []
        self._creator_badge_events_at: datetime | None = None
        # channel_id → last time Creator Badge GQL reported progress
        self._creator_badge_progress_at: dict[int, datetime] = {}

    async def get_session(self) -> aiohttp.ClientSession:
        if (session := self._session) is not None:
            if session.closed:
                raise RuntimeError("Session is closed")
            return session
        # load in cookies
        cookie_jar = aiohttp.CookieJar()
        try:
            if COOKIES_PATH.exists():
                cookie_jar.load(COOKIES_PATH)
        except Exception:
            # if loading in the cookies file ends up in an error, just ignore it
            # clear the jar, just in case
            cookie_jar.clear()
        # create timeouts
        # connection quality mulitiplier determines the magnitude of timeouts
        connection_quality = self.settings.connection_quality
        if connection_quality < 1:
            connection_quality = self.settings.connection_quality = 1
        elif connection_quality > 6:
            connection_quality = self.settings.connection_quality = 6
        timeout = aiohttp.ClientTimeout(
            sock_connect=5*connection_quality,
            total=10*connection_quality,
        )
        # create session, limited to 50 connections at maximum
        connector = aiohttp.TCPConnector(limit=50)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            cookie_jar=cookie_jar,
            headers={"User-Agent": self._client_type.USER_AGENT},
        )
        return self._session

    async def shutdown(self) -> None:
        start_time = time()
        self._pending_state = None
        self._pending_reason = ""
        await self.gui.inv.stop()
        await self.gui.stop_cache()
        self.stop_watching()
        for attribute in ("_watching_task", "_mnt_task"):
            task: asyncio.Task[None] | None = getattr(self, attribute)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
                setattr(self, attribute, None)
        # stop websocket, close session and save cookies
        await self.websocket.stop(clear_topics=True)
        if self._session is not None:
            cookie_jar = cast(aiohttp.CookieJar, self._session.cookie_jar)
            # clear empty cookie entries off the cookies file before saving
            # NOTE: Unfortunately, aiohttp provides no easy way of clearing empty cookies,
            # so we need to access the private '_cookies' attribute for this.
            for cookie_key, cookie in list(cookie_jar._cookies.items()):
                if not cookie:
                    del cookie_jar._cookies[cookie_key]
            cookie_jar.save(COOKIES_PATH)
            await self._session.close()
            self._session = None
        self._drops.clear()
        self.channels.clear()
        self.inventory.clear()
        self._campaigns.clear()
        self._auth_state.clear()
        self.wanted_games.clear()
        self._mnt_triggers.clear()
        self._deadline_triggers.clear()
        self._mining_paused = False
        self._skipped_channel_ids.clear()
        self._deferred_game_names.clear()
        self._campaign_detail_cache.clear()
        self._campaign_detail_sigs.clear()
        self._next_reload_at = None
        # wait at least half a second + whatever it takes to complete the closing
        # this allows aiohttp to safely close the session
        await asyncio.sleep(start_time + 0.5 - time())

    @staticmethod
    async def _cancel_tasks(tasks: abc.Iterable[asyncio.Task[Any]]) -> None:
        """Cancel tasks and consume their exceptions before closing a session."""
        task_list = list(tasks)
        for task in task_list:
            task.cancel()
        if task_list:
            await asyncio.gather(*task_list, return_exceptions=True)

    def wait_until_login(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._auth_state._logged_in.wait()

    @property
    def state(self) -> State:
        """The current lifecycle state, for both humans and tools."""
        return self._lifecycle.state

    @property
    def state_history(self) -> tuple[Transition[State], ...]:
        """Recent lifecycle transitions, oldest first."""
        return self._lifecycle.history

    def state_snapshot(self) -> dict[str, object]:
        """
        JSON-serializable snapshot of the miner lifecycle.

        Intended for tooling and agents: it answers "where am I, what can happen
        next, how did I get here, and why" in one call.
        """
        snapshot = self._lifecycle.as_dict()
        snapshot["pending"] = (
            self._pending_state.name if self._pending_state is not None else None
        )
        snapshot["pending_reason"] = self._pending_reason
        return snapshot

    def _state_hint(self) -> str:
        stamp = self._lifecycle.changed_at.astimezone().strftime("%X")
        hint = _("gui", "status", "since").format(time=stamp)
        if reason := self._lifecycle.reason:
            hint = f"{hint} • {reason}"
        if self._lifecycle.state is State.IDLE and self._next_reload_at is not None:
            reload_at = self._next_reload_at.astimezone().strftime("%X")
            hint = f"{hint} • {_('gui', 'status', 'next_reload').format(time=reload_at)}"
        return hint

    def _apply_state_ui(self) -> None:
        """
        Apply the user-visible side effects of the current lifecycle state.

        Keeping this in one place means the status bar and the tray icon can never
        disagree with the actual state, and that adding a state can't silently
        forget to update one of them.
        """
        state = self._lifecycle.state
        if (status := STATE_STATUS.get(state)) is not None:
            self.gui.status.update(_(*status))
        if (icon := STATE_TRAY_ICON.get(state)) is not None:
            self.gui.tray.change_icon(icon)
        self.gui.set_state_hint(self._state_hint())

    def change_state(self, state: State, *, reason: str = "") -> None:
        """
        Apply an immediate life-cycle transition.

        This is for the main pipeline and terminal EXIT. External code (GUI,
        websocket callbacks, timers) must use `request_state()` so an in-flight
        pipeline step cannot overwrite the request.

        `reason` is user-facing text shown under the status bar; pass a translated
        string when it explains *why* the app is in a waiting state (e.g. no
        campaigns). Pipeline steps can omit it.
        """
        if self._lifecycle.state is State.EXIT:
            # EXIT is terminal: keep the wake-up semantics, ignore the request.
            self._state_change.set()
            return
        previous = self._lifecycle.state
        changed = self._lifecycle.set(state, reason=reason)
        if changed:
            reason_text = f" ({reason})" if reason else ""
            logger.log(CALL, f"Lifecycle: {previous.name} -> {state.name}{reason_text}")
            self._apply_state_ui()
        self._state_change.set()

    def request_state(self, state: State, *, reason: str = "") -> None:
        """
        Request a life-cycle state from outside the main pipeline.

        Requests are latched rather than written straight into the state machine:
        a long `fetch_inventory()` must not overwrite a user's Reload/Exit, and a
        websocket event must not be lost between two pipeline steps.
        """
        if self._lifecycle.state is State.EXIT:
            return
        if state is State.EXIT:
            # EXIT is terminal and must be visible immediately.
            self._pending_state = None
            self._pending_reason = ""
            self.change_state(State.EXIT, reason=reason)
            return
        if self._pending_state is State.RESTART and state is not State.EXIT:
            # A restart (e.g. invalidating the token) must not be cancelled by a
            # later websocket notification or maintenance request.
            return
        if self._pending_state is not None:
            new_priority = self.REQUEST_PRIORITY.get(state, 0)
            old_priority = self.REQUEST_PRIORITY.get(self._pending_state, 0)
            if new_priority <= old_priority:
                # The existing request already covers this one (or is more urgent).
                return
            logger.log(
                CALL,
                f"Lifecycle request replaced: {self._pending_state.name} -> {state.name}",
            )
        self._pending_state = state
        self._pending_reason = reason
        self._state_change.set()

    def _apply_pending_state(self) -> None:
        state = self._pending_state
        if state is None:
            return
        if state is self.state:
            # The loop is about to run exactly this step; the request is already
            # satisfied and must not re-trigger it. Keep an explanatory reason
            # visible in the status hint.
            reason = self._pending_reason
            self._pending_state = None
            self._pending_reason = ""
            if reason:
                self.change_state(state, reason=reason)
            return
        if state in DEFERRED_REQUESTS and self.state not in SETTLED_STATES:
            # Do not short-circuit a running inventory pass. The pipeline reaches
            # CHANNELS_CLEANUP / CHANNEL_SWITCH on its own, where this request is
            # either satisfied by that step or applied afterwards.
            return
        reason = self._pending_reason
        self._pending_state = None
        self._pending_reason = ""
        self.change_state(state, reason=reason)

    def _advance_state(self, state: State, *, reason: str = "") -> bool:
        """
        Advance the automatic pipeline, yielding only to an urgent request.

        Deferred channel-level requests must not stall the pipeline (they are
        applied once it settles), so only an urgent pending request returns False.
        """
        if self._pending_state in URGENT_REQUESTS:
            return False
        self.change_state(state, reason=reason)
        return True

    def state_change(self, state: State) -> abc.Callable[[], None]:
        # this is identical to request_state, but defers the call
        # perfect for GUI usage
        return partial(self.request_state, state)

    def close(self):
        """
        Called when the application is requested to close by the user,
        usually by the console or application window being closed.
        """
        self.request_state(State.EXIT)

    def prevent_close(self):
        """
        Called when the application window has to be prevented from closing, even after the user
        closes it with X. Usually used solely to display tracebacks from the closing sequence.
        """
        self.gui.prevent_close()

    def print(self, message: str):
        """
        Can be used to print messages within the GUI.
        """
        self.gui.print(message)

    def save(self, *, force: bool = False) -> None:
        """
        Saves the application state.
        """
        self.gui.save(force=force)
        self.settings.save(force=force)

    def get_priority(self, channel: Channel) -> int:
        """
        Return a priority number for a given channel.

        0 has the highest priority.
        Higher numbers -> lower priority.
        MAX_INT (a really big number) signifies the lowest possible priority.
        """
        # Eligible Creator Badge targets outrank game channels whenever mining
        # them is allowed (interrupt always; fallback only with no finishable game).
        # Among badge targets, preserve discovery order (ends_at / remaining).
        if self._creator_badge_can_mine_channel(channel):
            login = channel.login.casefold()
            n = len(self._creator_badge_events)
            for i, event in enumerate(self._creator_badge_events):
                if event.channel_login.casefold() == login:
                    return -n + i
            return -1
        if (
            (game := channel.game) is None  # None when OFFLINE or no game set
            or game not in self.wanted_games  # we don't care about the played game
        ):
            return MAX_INT
        return self.wanted_games.index(game)

    @staticmethod
    def _viewers_key(channel: Channel) -> int:
        if (viewers := channel.viewers) is not None:
            return viewers
        return -1

    async def run(self):
        if self.settings.dump:
            with open(DUMP_PATH, 'w', encoding="utf8"):
                # replace the existing file with an empty one
                pass
        while True:
            try:
                await self._run()
                break
            except ReloadRequest:
                await self.shutdown()
            except ExitRequest:
                break
            except aiohttp.ContentTypeError as exc:
                raise RequestException(_("login", "unexpected_content")) from exc

    def _idle_reason(self) -> tuple[str, str]:
        """
        Explain why no games are being mined.

        Returns (output message, short status-bar reason). The distinction matters:
        "no campaigns at all" and "you haven't picked any priority games" need
        different user actions.
        """
        if not self.inventory:
            return _("status", "no_campaign"), _("gui", "status", "no_campaign_reason")
        if (
            self.settings.priority_mode is PriorityMode.PRIORITY_ONLY
            and not self.settings.priority
        ):
            return _("status", "no_priority"), _("gui", "status", "no_priority_reason")
        return (
            _("status", "no_matching_campaigns"),
            _("gui", "status", "no_matching_reason"),
        )

    @task_wrapper
    async def _check_update(self, *, interactive: bool = False) -> None:
        """Check GitHub for a newer Windows build; optionally download and apply it."""
        session = await self.get_session()
        info = await fetch_update_info(session)
        if info is None:
            msg = _("status", "update_check_failed")
            self.print(msg)
            if interactive:
                self.gui.show_error(_("gui", "help", "update", "failed_title"), msg)
            return
        if not info.is_newer:
            msg = _("status", "update_up_to_date")
            self.print(msg)
            if interactive:
                self.gui.show_info(_("gui", "help", "update", "up_to_date_title"), msg)
            return

        available_msg = _("status", "update_available").format(
            published=info.published_at or "?",
            sha=info.remote_sha[:7] if info.remote_sha else "?",
        )
        self.print(available_msg)
        # Tray toast so background/minimized sessions notice a new build.
        self.gui.tray.notify(available_msg, _("gui", "help", "update", "title"))
        if not can_auto_apply():
            msg = _("status", "update_manual")
            self.print(msg)
            if interactive:
                self.gui.show_info(_("gui", "help", "update", "title"), msg)
            return

        # Startup: only auto-install when the setting is on. Manual button always asks.
        if not interactive and not self.settings.auto_update:
            return

        should_install = True
        if interactive or self.settings.auto_update:
            should_install = self.gui.ask_yes_no(
                _("gui", "help", "update", "title"),
                _("gui", "help", "update", "confirm").format(
                    published=info.published_at or "?",
                    sha=info.remote_sha[:7] if info.remote_sha else "?",
                ),
            )
        if not should_install:
            return

        await self._install_update(info)

    async def _install_update(self, info: UpdateInfo) -> None:
        from constants import WORKING_DIR

        self.print(_("status", "update_downloading"))
        session = await self.get_session()
        zip_path = WORKING_DIR / "_update_download.zip"
        try:
            await download_update(session, info, zip_path)
            self.print(_("status", "update_applying"))
            apply_windows_update(
                zip_path, remote_sha=info.remote_sha, release_notes=info.release_notes
            )
            # Soft-close first, then hard-exit so Windows releases the EXE lock.
            # The detached .bat waits for our PID before copying (see updater.py).
            self.gui.close()
            import threading
            from time import sleep as _sleep

            def _force_exit_for_update() -> None:
                # Give shutdown a moment, then hard-exit so EXE/DLL locks drop.
                _sleep(1.5)
                os._exit(0)

            threading.Thread(target=_force_exit_for_update, daemon=True).start()
        except Exception:
            logger.exception("Failed to apply update")
            msg = _("status", "update_failed")
            self.print(msg)
            self.gui.show_error(_("gui", "help", "update", "failed_title"), msg)

    def request_update_check(self) -> None:
        asyncio.create_task(self._check_update(interactive=True))

    def _notify_update_success_if_needed(self) -> None:
        result = consume_update_success()
        if not result:
            return
        sha, notes = result
        short = sha[:7] if sha != "unknown" else sha
        msg = _("status", "update_success").format(sha=short)
        if notes:
            msg = f"{msg}\n\n{notes}"
        self.print(_("status", "update_success").format(sha=short))
        self.gui.show_info(_("gui", "help", "update", "success_title"), msg)
        self.gui.tray.notify(msg.split("\n", 1)[0], _("gui", "help", "update", "success_title"))

    async def _run(self):
        """
        Main method that runs the whole client.

        Here, we manage several things, specifically:
        • Fetching the drops inventory to make sure that everything we can claim, is claimed
        • Selecting a stream to watch, and watching it
        • Changing the stream that's being watched if necessary
        """
        self.gui.start()
        if self.state is State.EXIT:
            return
        # Defer so the main window is mapped before the modal dialog.
        self.gui._root.after(500, self._notify_update_success_if_needed)
        auth_state = await self.get_auth()
        if self.state is State.EXIT:
            return
        if self.settings.auto_update:
            asyncio.create_task(self._check_update(interactive=False))
        await self.websocket.start()
        if self.state is State.EXIT:
            return
        # NOTE: watch task is explicitly restarted on each new run
        if self._watching_task is not None:
            await self._cancel_tasks([self._watching_task])
        self._watching_task = asyncio.create_task(self._watch_loop())
        # Add default topics
        self.websocket.add_topics([
            WebsocketTopic("User", "Drops", auth_state.user_id, self.process_drops),
            WebsocketTopic(
                "User", "Notifications", auth_state.user_id, self.process_notifications
            ),
        ])
        full_cleanup: bool = False
        channels: Final[OrderedDict[int, Channel]] = self.channels
        # A ReloadRequest exits `_run` with the machine in RESTART. Reset it
        # explicitly so the next run always starts from a known state.
        if self.state is not State.EXIT:
            self._lifecycle.reset(reason="new run")
            # Requests made before the first fetch are subsumed by that fetch.
            # RESTART is kept: it is the token-invalidation/reload path.
            if self._pending_state is not None and self._pending_state is not State.RESTART:
                logger.log(
                    CALL,
                    f"Dropping pre-start request: {self._pending_state.name}",
                )
                self._pending_state = None
                self._pending_reason = ""
        self.change_state(State.INVENTORY_FETCH)
        while True:
            self._apply_pending_state()
            if self.state is State.IDLE:
                if self.settings.dump:
                    self.gui.close()
                    continue
                self.stop_watching()
                # clear the flag and wait until it's set again
                self._state_change.clear()
            elif self.state is State.INVENTORY_FETCH:
                # A fresh inventory can remove campaigns and their ACL channels,
                # so this pass must clean up every channel, not just stale ones.
                full_cleanup = True
                # ensure the websocket is running
                await self.websocket.start()
                await self.fetch_inventory()
                self.gui.set_games(set(campaign.game for campaign in self.inventory))
                # Save state on every inventory fetch
                self.save()
                self._advance_state(State.GAMES_UPDATE)
            elif self.state is State.RULES_UPDATE:
                # Priority/exclude/priority-mode changes are local. Recompute the
                # wanted-games list and rebuild channels without a GQL reload.
                # NOTE: Do not clear `full_cleanup` here. If a RULES_UPDATE
                # preempted the tail of an INVENTORY_FETCH, the fresh inventory
                # may have removed campaigns, and their ACL channels must still
                # be cleaned up. The flag is cleared after cleanup runs.
                self.change_state(State.GAMES_UPDATE)
            elif self.state is State.GAMES_UPDATE:
                # claim drops from expired and active campaigns
                for campaign in self.inventory:
                    if not campaign.upcoming:
                        for drop in campaign.drops:
                            if drop.can_claim:
                                await drop.claim()
                # figure out which games we want
                self.wanted_games.clear()
                exclude = self.settings.exclude
                priority = self.settings.priority
                priority_mode = self.settings.priority_mode
                priority_only = priority_mode is PriorityMode.PRIORITY_ONLY
                next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
                # sorted_campaigns: list[DropsCampaign] = list(self.inventory)
                sorted_campaigns: list[DropsCampaign] = self.inventory
                if not priority_only:
                    if priority_mode is PriorityMode.ENDING_SOONEST:
                        sorted_campaigns.sort(key=lambda c: c.ends_at)
                    elif priority_mode is PriorityMode.LOW_AVBL_FIRST:
                        sorted_campaigns.sort(key=lambda c: c.availability)
                sorted_campaigns.sort(
                    key=lambda c: (
                        priority.index(c.game.name) if c.game.name in priority else MAX_INT
                    )
                )
                # Campaigns that can still be completed in time are preferred. Ones that
                # can't are only used if there is nothing else to mine.
                for require_finishable in (True, False):
                    for campaign in sorted_campaigns:
                        game: Game = campaign.game
                        if (
                            game not in self.wanted_games  # isn't already there
                            # and isn't excluded by list or priority mode
                            and game.name not in exclude
                            and (not priority_only or game.name in priority)
                            # and can be progressed within the next hour
                            and campaign.can_earn_within(next_hour)
                            and (not require_finishable or campaign.can_finish())
                        ):
                            # non-excluded games with no priority are placed last,
                            # below priority ones
                            self.wanted_games.append(game)
                    if self.wanted_games:
                        break
                # Session "Next game": keep deferred games, but after everything else
                if self._deferred_game_names and len(self.wanted_games) > 1:
                    preferred = [
                        g for g in self.wanted_games if g.name not in self._deferred_game_names
                    ]
                    deferred = [
                        g for g in self.wanted_games if g.name in self._deferred_game_names
                    ]
                    if preferred:
                        self.wanted_games = preferred + deferred
                # After the priority list is exhausted, optionally keep mining
                # badge/emote-only campaigns even when Priority-only is enabled.
                if self.settings.fallback_badges_emotes:
                    priority_still_active = any(
                        campaign.game.name in priority
                        and campaign.game.name not in exclude
                        and campaign.can_earn_within(next_hour)
                        and not campaign.finished
                        for campaign in self.inventory
                    )
                    if not priority_still_active:
                        for campaign in sorted_campaigns:
                            game = campaign.game
                            if (
                                campaign.only_badges_or_emotes
                                and game not in self.wanted_games
                                and game.name not in exclude
                                and campaign.can_earn_within(next_hour)
                            ):
                                self.wanted_games.append(game)
                full_cleanup = True
                self.restart_watching()
                self._advance_state(State.CHANNELS_CLEANUP)
            elif self.state is State.CHANNELS_CLEANUP:
                badge_ids = self._creator_badge_channel_ids()
                if not self.wanted_games or full_cleanup:
                    # no games selected or we're doing full cleanup: remove everything
                    # (Creator Badge targets are re-added in CHANNELS_FETCH)
                    to_remove_channels: list[Channel] = list(channels.values())
                else:
                    # Incremental cleanup must still drop channels whose game is
                    # no longer wanted, including ACL-based ones; otherwise a
                    # deleted priority game stays in the list until the next full
                    # reload. Offline ACL channels are kept because they can come
                    # online later and must stay subscribed. Creator Badge targets
                    # are kept the same way until dropped from `_creator_badge_events`.
                    to_remove_channels = [
                        channel
                        for channel in channels.values()
                        if channel.id not in badge_ids
                        and (
                            (channel.game is not None and channel.game not in self.wanted_games)
                            or (
                                not channel.acl_based
                                and (channel.offline or channel.game is None)
                            )
                        )
                    ]
                full_cleanup = False
                if to_remove_channels:
                    to_remove_topics: list[str] = []
                    for channel in to_remove_channels:
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamState", channel.id)
                        )
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id)
                        )
                    self.websocket.remove_topics(to_remove_topics)
                    for channel in to_remove_channels:
                        del channels[channel.id]
                        channel.remove()
                    del to_remove_channels, to_remove_topics
                if self.wanted_games or self._should_track_creator_badge_channels():
                    self._advance_state(State.CHANNELS_FETCH)
                else:
                    # with no eligible games, explain exactly why and wait
                    message, hint = self._idle_reason()
                    self.print(message)
                    self._advance_state(State.IDLE, reason=hint)
            elif self.state is State.CHANNELS_FETCH:
                # Keep the channel map + GUI alive during revalidation so websocket
                # events still resolve and the Treeview can be reconciled (diff)
                # instead of cleared and rebuilt.
                new_channels: set[Channel] = set(channels.values())
                prior_ids: set[int] = set(channels)
                # gather and add ACL channels from campaigns
                # NOTE: we consider only campaigns that can be progressed
                # NOTE: we use another set so that we can set them online separately
                no_acl: set[Game] = set()
                acl_channels: set[Channel] = set()
                next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
                for campaign in self.inventory:
                    if (
                        campaign.game in self.wanted_games
                        and campaign.can_earn_within(next_hour)
                    ):
                        if campaign.allowed_channels:
                            acl_channels.update(campaign.allowed_channels)
                        else:
                            no_acl.add(campaign.game)
                # remove all ACL channels that already exist from the other set
                acl_channels.difference_update(new_channels)
                # Creator Badge follow targets (may not appear in any campaign ACL)
                badge_channels: set[Channel] = set()
                if self._should_track_creator_badge_channels():
                    existing_ids = {ch.id for ch in new_channels} | {ch.id for ch in acl_channels}
                    for event in self._creator_badge_events:
                        if event.channel_id in existing_ids:
                            continue
                        badge_channels.add(
                            Channel(
                                self,
                                id=event.channel_id,
                                login=event.channel_login,
                            )
                        )
                # Revalidate every tracked channel, not just newly discovered ACL
                # ones. A missed websocket `stream-down` must not leave a channel
                # ONLINE forever; `bulk_check_online` now applies offline results too.
                await self.bulk_check_online(new_channels | acl_channels | badge_channels)
                # finally, add them as new channels
                new_channels.update(acl_channels)
                new_channels.update(badge_channels)
                # for every campaign without an ACL, for its game, add a list of
                # live channels with drops enabled. These directory queries are
                # independent, so run them together instead of waiting one by one.
                if no_acl:
                    live_stream_tasks = [
                        asyncio.create_task(
                            self.get_live_streams(game, drops_enabled=True)
                        )
                        for game in no_acl
                    ]
                    try:
                        live_stream_lists = await asyncio.gather(*live_stream_tasks)
                    except BaseException:
                        await self._cancel_tasks(live_stream_tasks)
                        raise
                    for live_streams in live_stream_lists:
                        new_channels.update(live_streams)
                # sort them descending by viewers, by priority and by game priority
                # NOTE: Viewers sort also ensures ONLINE channels are sorted to the top
                # NOTE: We can drop using the set now, because there's no more channels being added
                ordered_channels: list[Channel] = sorted(
                    new_channels, key=self._viewers_key, reverse=True
                )
                ordered_channels.sort(key=lambda ch: ch.acl_based, reverse=True)
                ordered_channels.sort(key=self.get_priority)
                blocked = self.settings.channel_exclude
                if blocked:
                    ordered_channels = [
                        channel for channel in ordered_channels
                        if channel.login.casefold() not in blocked
                    ]
                # ensure that we won't end up with more channels than we can handle
                # NOTE: we trim from the end because that's where the non-priority,
                # offline (or online but low viewers) channels end up
                ordered_channels = ordered_channels[:MAX_CHANNELS]
                ordered_ids = {channel.id for channel in ordered_channels}
                # Unsubscribe + drop channels that fell out of the list
                dropped = [ch for ch in list(channels.values()) if ch.id not in ordered_ids]
                if dropped:
                    to_remove_topics = []
                    for channel in dropped:
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamState", channel.id)
                        )
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id)
                        )
                        del channels[channel.id]
                    self.websocket.remove_topics(to_remove_topics)
                    del dropped, to_remove_topics
                # Update map, then reconcile the Treeview (keeps selection/scroll)
                for channel in ordered_channels:
                    channels[channel.id] = channel
                self.gui.channels.reconcile(ordered_channels)
                # Subscribe only to newly added channels
                added_ids = ordered_ids - prior_ids
                if added_ids:
                    to_add_topics: list[WebsocketTopic] = []
                    for channel_id in added_ids:
                        to_add_topics.append(
                            WebsocketTopic(
                                "Channel",
                                "StreamState",
                                channel_id,
                                self.process_stream_state,
                            )
                        )
                        to_add_topics.append(
                            WebsocketTopic(
                                "Channel",
                                "StreamUpdate",
                                channel_id,
                                self.process_stream_update,
                            )
                        )
                    self.websocket.add_topics(to_add_topics)
                    del to_add_topics
                del prior_ids, ordered_ids, added_ids
                # relink watching channel after cleanup,
                # or stop watching it if it no longer qualifies
                # NOTE: this replaces 'self.watching_channel's internal value with the new object
                watching_channel = self.watching_channel.get_with_default(None)
                if watching_channel is not None:
                    new_watching: Channel | None = channels.get(watching_channel.id)
                    if new_watching is not None and self.can_watch(new_watching):
                        self.watch(new_watching, update_status=False)
                    else:
                        # we've removed a channel we were watching
                        self.stop_watching()
                    del new_watching
                # pre-display the active drop with a substracted minute
                for channel in channels.values():
                    # check if there's any channels we can watch first
                    if self.can_watch(channel):
                        if (
                            (active_campaign := self.get_active_campaign(channel)) is not None
                            and (active_drop := active_campaign.first_drop) is not None
                        ):
                            active_drop.display(countdown=False, subone=True)
                        break
                self._advance_state(State.CHANNEL_SWITCH)
                del (
                    no_acl,
                    acl_channels,
                    new_channels,
                    ordered_channels,
                    watching_channel,
                )
            elif self.state is State.CHANNEL_SWITCH:
                if self.settings.dump:
                    self.gui.close()
                    continue
                if self._mining_paused:
                    self.stop_watching()
                    self.gui.status.update(_("gui", "status", "paused"))
                    self.gui.set_state_hint(_("status", "paused"))
                    self._state_change.clear()
                    continue
                # Change into the selected channel, stay in the watching channel,
                # or select a new channel that meets the required conditions
                new_watching = None
                selected_channel = self.gui.channels.get_selection()
                if selected_channel is not None and self.can_watch(selected_channel):
                    # selected channel is checked first, and set as long as we can watch it
                    new_watching = selected_channel
                else:
                    # other channels additionally need to have a good reason
                    # for a switch (including the watching one)
                    # NOTE: we need to sort the channels every time because one channel
                    # can end up streaming any game - channels aren't game-tied
                    for channel in sorted(channels.values(), key=self.get_priority):
                        if self.should_switch(channel):
                            new_watching = channel
                            break
                watching_channel = self.watching_channel.get_with_default(None)
                if new_watching is not None:
                    # if we have a better switch target - do so
                    self.watch(new_watching)
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                elif watching_channel is not None and self.can_watch(watching_channel):
                    # otherwise, continue watching what we had before
                    self.gui.status.update(self._watching_status_text(watching_channel))
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                else:
                    # not watching anything and there isn't anything to watch either
                    no_channel_text = _("status", "no_channel")
                    self.print(no_channel_text)
                    self._advance_state(
                        State.IDLE, reason=_("gui", "status", "no_channel_reason")
                    )
                del new_watching, selected_channel, watching_channel
            elif self.state is State.RESTART:
                raise ReloadRequest()
            elif self.state is State.EXIT:
                # we've been requested to exit the application
                break
            await self._state_change.wait()

    async def _watch_sleep(self, delay: float) -> None:
        # use wait_for here to allow an asyncio.sleep-like that can be ended prematurely
        # NOTE: check the flag first, so a restart requested while the watch payload
        # was in flight isn't lost by clearing it before waiting.
        if self._watching_restart.is_set():
            self._watching_restart.clear()
            return
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._watching_restart.wait(), timeout=delay)
        self._watching_restart.clear()

    @task_wrapper(critical=True)
    async def _watch_loop(self) -> NoReturn:
        interval: float = WATCH_INTERVAL.total_seconds()
        while True:
            channel: Channel = await self.watching_channel.get()
            if not channel.online:
                # if the channel isn't online anymore, we stop watching it
                self.stop_watching()
                continue
            # logger.log(CALL, f"Sending watch payload to: {channel.name}")
            last_sent: float = time()
            succeeded: bool = await channel.send_watch()
            if not succeeded:
                logger.log(CALL, f"Watch requested failed for channel: {channel.name}")
            # wait ~15 seconds for a progress update
            await asyncio.sleep(15 - min(time() - last_sent, 15))
            if self.gui.progress.minute_almost_done():
                # If the previous update was more than ~60s ago, and the progress tracker
                # isn't counting down anymore, that means Twitch has temporarily
                # stopped reporting drop's progress. To ensure the timer keeps at least somewhat
                # accurate time, we can use GQL to query for the current drop,
                # or even "pretend" mining as a last resort option.
                handled: bool = False

                # Solution 1: use GQL to query for the currently mined drop status
                try:
                    context = await self.gql_request(
                        GQL_QUERIES["CurrentDrop"].with_variables(
                            {"channelID": str(channel.id)}
                        )
                    )
                    drop_data: JsonType | None = (
                        context["data"]["currentUser"]["dropCurrentSession"]
                    )
                except GQLException:
                    drop_data = None
                if drop_data is not None:
                    gql_drop: TimedDrop | None = self._drops.get(drop_data["dropID"])
                    if gql_drop is not None and gql_drop.can_earn(channel):
                        gql_drop.update_minutes(drop_data["currentMinutesWatched"])
                        drop_text: str = (
                            f"{gql_drop.name} ({gql_drop.campaign.game}, "
                            f"{gql_drop.current_minutes}/{gql_drop.required_minutes})"
                        )
                        logger.log(CALL, f"Drop progress from GQL: {drop_text}")
                        handled = True

                # Solution 2: If GQL fails, figure out which campaign we're most likely mining
                # right now, and then bump up the minutes on it's drops
                if not handled:
                    if (active_campaign := self.get_active_campaign(channel)) is not None:
                        active_campaign.bump_minutes(channel)
                        # NOTE: This usually gets overwritten below
                        drop_text = f"Unknown drop ({active_campaign.game})"
                        if (active_drop := active_campaign.first_drop) is not None:
                            active_drop.display()
                            drop_text = (
                                f"{active_drop.name} ({active_drop.campaign.game}, "
                                f"{active_drop.current_minutes}/{active_drop.required_minutes})"
                            )
                        logger.log(CALL, f"Drop progress from active search: {drop_text}")
                        handled = True
                    else:
                        logger.log(CALL, "No active drop could be determined")
            # Creator Badge watch-time progress (independent of game-drop GQL)
            await self._update_creator_badge_progress(channel)
            await self._watch_sleep(interval - min(time() - last_sent, interval))

    @task_wrapper(critical=True)
    async def _maintenance_task(self) -> None:
        now = datetime.now(timezone.utc)
        next_period = now + timedelta(hours=1)
        while True:
            # exit if there's no need to repeat the loop
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            next_trigger = next_period
            while self._mnt_triggers and self._mnt_triggers[0] <= next_trigger:
                next_trigger = self._mnt_triggers.popleft()
            trigger_type: str = "Reload" if next_trigger == next_period else "Cleanup"
            logger.log(
                CALL,
                (
                    "Maintenance task waiting until: "
                    f"{next_trigger.astimezone().strftime('%X')} ({trigger_type})"
                )
            )
            await asyncio.sleep((next_trigger - now).total_seconds())
            # exit after waiting, before the actions
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            if next_trigger in self._deadline_triggers:
                logger.log(CALL, "Maintenance task requests a games update (deadline)")
                self.change_state(State.GAMES_UPDATE)
            elif next_trigger != next_period:
                logger.log(CALL, "Maintenance task requests channels cleanup")
                self.request_state(State.CHANNELS_CLEANUP)
        # this triggers a restart of this task every (up to) 60 minutes
        logger.log(CALL, "Maintenance task requests a reload")
        self.request_state(State.INVENTORY_FETCH)

    def can_watch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a watching candidate.
        """
        # exit early if stream is offline
        if not channel.online:
            return False
        if channel.login.casefold() in self.settings.channel_exclude:
            return False
        if self._mining_paused or channel.id in self._skipped_channel_ids:
            return False
        if self._creator_badge_can_mine_channel(channel):
            return True
        return self._can_watch_for_game_campaign(channel)

    def _can_watch_for_game_campaign(self, channel: Channel) -> bool:
        """Game-drop eligibility only (no Creator Badge short-circuit)."""
        for campaign in self.inventory:
            if (
                campaign.can_earn(channel)  # let the campaign do the "special games" check
                and (
                    # limit watching to the games the user wants
                    channel.game is not None
                    and channel.drops_enabled
                    and channel.game in self.wanted_games
                    # let the campaign ignore all channel-related checks
                    or campaign.game.is_special()
                )
            ):
                return True
        return False

    def should_switch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a switch candidate.
        """
        if not self.can_watch(channel):
            return False
        watching_channel = self.watching_channel.get_with_default(None)
        if watching_channel is None or not self.can_watch(watching_channel):
            return True
        channel_order = self.get_priority(channel)
        watching_order = self.get_priority(watching_channel)
        return (
            # this channel's game is higher order than the watching one's
            channel_order < watching_order
            or channel_order == watching_order  # or the order is the same
            # and this channel is ACL-based and the watching channel isn't
            and channel.acl_based > watching_channel.acl_based
        )

    def watch(self, channel: Channel, *, update_status: bool = True):
        self.gui.tray.change_icon("active")
        self.gui.channels.set_watching(channel)
        self.watching_channel.set(channel)
        if update_status:
            status_text = self._watching_status_text(channel)
            self.print(status_text)
            self.gui.status.update(status_text)
        if self._creator_badge_can_mine_channel(channel):
            self._creator_badge_progress_at.setdefault(
                channel.id, datetime.now(timezone.utc)
            )
        # Warn once when mining a campaign that Twitch reports as unlinked —
        # progress/claim for game entitlements may still fail server-side.
        campaign = self.get_active_campaign(channel)
        if (
            campaign is not None
            and not campaign.linked
            and not campaign.only_badges_or_emotes
            and campaign.id not in self._unlinked_warn_ids
        ):
            self._unlinked_warn_ids.add(campaign.id)
            self.print(
                _("status", "unlinked_warning").format(
                    campaign=campaign.name, game=campaign.game.name
                )
            )

    def stop_watching(self):
        self.gui.clear_drop()
        self.watching_channel.clear()
        self.gui.channels.clear_watching()

    def restart_watching(self):
        self.gui.progress.stop_timer()
        self._watching_restart.set()

    @property
    def is_paused(self) -> bool:
        return self._mining_paused

    def toggle_pause(self) -> None:
        """Tray/GUI: pause or resume mining without quitting."""
        self._mining_paused = not self._mining_paused
        if self._mining_paused:
            self.stop_watching()
            self.print(_("status", "paused"))
            self.gui.status.update(_("gui", "status", "paused"))
            self.gui.set_state_hint(_("status", "paused"))
            self.gui.tray.change_icon("idle")
            self.request_state(State.CHANNEL_SWITCH)
        else:
            self.print(_("status", "resumed"))
            self.gui.set_state_hint("")
            self.request_state(State.CHANNEL_SWITCH)
        self.gui.status.refresh_pause_button()

    def exclude_channel(self, channel: Channel) -> None:
        """Permanently skip one channel. Games stay minable on other channels."""
        login = channel.login.casefold()
        if login not in self.settings.channel_exclude:
            self.settings.channel_exclude.add(login)
            self.settings.alter()
        tracked = self.channels.get(channel.id)
        if tracked is not None:
            self.websocket.remove_topics([
                WebsocketTopic.as_str("Channel", "StreamState", channel.id),
                WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id),
            ])
            del self.channels[channel.id]
            tracked.remove()
        watching = self.watching_channel.get_with_default(None)
        if watching is not None and watching.id == channel.id:
            self.stop_watching()
        self.print(_("status", "channel_excluded").format(channel=channel.name))
        if self.gui.settings is not None:
            self.gui.settings.refresh_channel_exclude()
        self.request_state(State.CHANNEL_SWITCH)

    def skip_watching_channel(self) -> None:
        """Tray: skip the current channel for this session."""
        channel = self.watching_channel.get_with_default(None)
        if channel is None:
            self.print(_("status", "skip_nothing"))
            return
        self._skipped_channel_ids.add(channel.id)
        self.print(_("status", "skipped_channel").format(channel=channel.name))
        self.stop_watching()
        self.request_state(State.CHANNEL_SWITCH)

    def skip_watching_game(self) -> None:
        """Tray: defer the current game until other wanted games are done."""
        channel = self.watching_channel.get_with_default(None)
        if channel is None or channel.game is None:
            self.print(_("status", "skip_nothing"))
            return
        game = channel.game
        self._deferred_game_names.add(game.name)
        if game in self.wanted_games and len(self.wanted_games) > 1:
            self.wanted_games = [g for g in self.wanted_games if g != game] + [game]
        self.print(_("status", "skipped_game").format(game=game.name))
        self.stop_watching()
        self.request_state(State.CHANNEL_SWITCH)

    @task_wrapper
    async def process_stream_state(self, channel_id: int, message: JsonType):
        msg_type = message["type"]
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Stream state change for a non-existing channel: {channel_id}")
            return
        if msg_type == "viewcount":
            if not channel.online:
                # if it's not online for some reason, set it so
                channel.check_online()
            else:
                viewers = message["viewers"]
                channel.viewers = viewers
                channel.display()
                # logger.debug(f"{channel.name} viewers: {viewers}")
        elif msg_type == "stream-down":
            channel.set_offline()
        elif msg_type == "stream-up":
            channel.check_online()
        elif msg_type == "commercial":
            # skip these
            pass
        else:
            logger.warning(f"Unknown stream state: {msg_type}")

    @task_wrapper
    async def process_stream_update(self, channel_id: int, message: JsonType):
        # message = {
        #     "channel_id": "12345678",
        #     "type": "broadcast_settings_update",
        #     "channel": "channel._login",
        #     "old_status": "Old title",
        #     "status": "New title",
        #     "old_game": "Old game name",
        #     "game": "New game name",
        #     "old_game_id": 123456,
        #     "game_id": 123456
        # }
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Broadcast settings update for a non-existing channel: {channel_id}")
            return
        if message["old_game"] != message["game"]:
            game_change = f", game changed: {message['old_game']} -> {message['game']}"
        else:
            game_change = ''
        logger.log(CALL, f"Channel update from websocket: {channel.name}{game_change}")
        # There's no information about channel tags here, but this event is triggered
        # when the tags change. We can use this to just update the stream data after the change.
        # Use 'check_online' to introduce a delay, allowing for multiple title and tags
        # changes before we update. This eventually calls 'on_channel_update' below.
        channel.check_online()

    def on_channel_update(
        self, channel: Channel, stream_before: Stream | None, stream_after: Stream | None
    ):
        """
        Called by a Channel when it's status is updated (ONLINE, OFFLINE, title/tags change).

        NOTE: 'stream_before' gets dealocated once this function finishes.
        """
        if stream_before is None:
            if stream_after is not None:
                # Channel going ONLINE
                if self.should_switch(channel):
                    # we can watch the channel, and we should
                    self.print(_("status", "goes_online").format(channel=channel.name))
                    if self._lifecycle.state is State.IDLE:
                        # Do not start the watch loop while the UI says Idle;
                        # request a switch so the pipeline owns the transition.
                        self.request_state(State.CHANNEL_SWITCH)
                    else:
                        self.watch(channel)
                else:
                    logger.info(f"{channel.name} goes ONLINE")
            else:
                # Channel was OFFLINE and stays that way
                logger.log(CALL, f"{channel.name} stays OFFLINE")
        else:
            watching_channel = self.watching_channel.get_with_default(None)
            # check if the watching channel was the one updated
            if watching_channel is not None and watching_channel == channel:
                # NOTE: In these cases, channel was the watching channel
                if not self.can_watch(channel):
                    # we can't watch it anymore
                    if stream_after is None:
                        # Channel going OFFLINE
                        self.print(_("status", "goes_offline").format(channel=channel.name))
                    else:
                        # Channel stays ONLINE, but we can't watch it anymore
                        logger.info(
                            f"{channel.name} status has been updated, switching... "
                            f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                            f"{stream_after.drops_enabled and '✔' or '❌'})"
                        )
                    self.request_state(State.CHANNEL_SWITCH)
                else:
                    # Channel stays ONLINE, and we can still watch it - no change
                    pass
            # NOTE: In these cases, it wasn't the watching channel
            elif stream_after is None:
                logger.info(f"{channel.name} goes OFFLINE")
            else:
                # Channel stays ONLINE, but has been updated
                logger.info(
                    f"{channel.name} status has been updated "
                    f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                    f"{stream_after.drops_enabled and '✔' or '❌'})"
                )
                if self.should_switch(channel):
                    # ... and we should watch it
                    if self._lifecycle.state is State.IDLE:
                        self.request_state(State.CHANNEL_SWITCH)
                    else:
                        self.watch(channel)
        channel.display()

    @task_wrapper
    async def process_drops(self, user_id: int, message: JsonType):
        # Message examples:
        # {"type": "drop-progress", data: {"current_progress_min": 3, "required_progress_min": 10}}
        # {"type": "drop-claim", data: {"drop_instance_id": ...}}
        msg_type: str = message["type"]
        if msg_type not in ("drop-progress", "drop-claim"):
            return
        drop_id: str = message["data"]["drop_id"]
        drop: TimedDrop | None = self._drops.get(drop_id)
        watching_channel: Channel | None = self.watching_channel.get_with_default(None)
        if msg_type == "drop-claim":
            if drop is None:
                logger.error(
                    f"Received a drop claim ID for a non-existing drop: {drop_id}\n"
                    f"Drop claim ID: {message['data']['drop_instance_id']}"
                )
                return
            drop.update_claim(message["data"]["drop_instance_id"])
            campaign = drop.campaign
            await drop.claim()
            drop.display()
            # About 4-20s after claiming the drop, next drop can be started
            # by re-sending the watch payload. We can test for it by fetching the current drop
            # via GQL, and then comparing drop IDs.
            await asyncio.sleep(4)
            if watching_channel is not None:
                for attempt in range(8):
                    context = await self.gql_request(
                        GQL_QUERIES["CurrentDrop"].with_variables(
                            {"channelID": str(watching_channel.id)}
                        )
                    )
                    drop_data: JsonType | None = (
                        context["data"]["currentUser"]["dropCurrentSession"]
                    )
                    if drop_data is None or drop_data["dropID"] != drop.id:
                        break
                    await asyncio.sleep(2)
            if campaign.can_earn(watching_channel):
                self.restart_watching()
            else:
                self.request_state(State.INVENTORY_FETCH)
            return
        assert msg_type == "drop-progress"
        if drop is not None:
            drop_text = (
                f"{drop.name} ({drop.campaign.game}, "
                f"{message['data']['current_progress_min']}/"
                f"{message['data']['required_progress_min']})"
            )
        else:
            drop_text = "<Unknown>"
        logger.log(CALL, f"Drop update from websocket: {drop_text}")
        if drop is not None and drop.can_earn(self.watching_channel.get_with_default(None)):
            # the received payload is for the drop we expected
            drop.update_minutes(message["data"]["current_progress_min"])

    @task_wrapper
    async def process_notifications(self, user_id: int, message: JsonType):
        if message["type"] == "create-notification":
            data: JsonType = message["data"]["notification"]
            if data["type"] in (
                "user_drop_reward_reminder_notification",  # drop confirmation
                "quests_viewer_reward_campaign_earned_emote",  # emote confirmation
                # badge confirmation?
            ):
                self.request_state(State.INVENTORY_FETCH)
                await self.gql_request(
                    GQL_QUERIES["NotificationsDelete"].with_variables(
                        {"input": {"id": data["id"]}}
                    )
                )

    async def get_auth(self) -> _AuthState:
        await self._auth_state.validate()
        return self._auth_state

    @asynccontextmanager
    async def request(
        self, method: str, url: URL | str, *, invalidate_after: datetime | None = None, **kwargs
    ) -> abc.AsyncIterator[aiohttp.ClientResponse]:
        session = await self.get_session()
        method = method.upper()
        if self.settings.proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self.settings.proxy
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Request: (method=%s, url=%s, kwargs=%s)",
                method,
                redact_url(url),
                redact_sensitive(kwargs),
            )
        session_timeout = timedelta(seconds=session.timeout.total or 0)
        backoff = ExponentialBackoff(maximum=3*60)
        for delay in backoff:
            if self.gui.close_requested:
                raise ExitRequest()
            elif (
                invalidate_after is not None
                # account for the expiration landing during the request
                and datetime.now(timezone.utc) >= (invalidate_after - session_timeout)
            ):
                raise RequestInvalid()
            try:
                response: aiohttp.ClientResponse | None = None
                response = await self.gui.coro_unless_closed(
                    session.request(method, url, **kwargs)
                )
                assert response is not None
                logger.debug(
                    "Response: %s: %s", response.status, redact_url(response.url)
                )
                if response.status < 500:
                    # pre-read the response to avoid getting errors outside of the context manager
                    raw_response = await response.read()  # noqa
                    yield response
                    return
                self.print(_("error", "site_down").format(seconds=round(delay)))
            except aiohttp.ClientConnectorCertificateError:
                # for a case where SSL verification fails
                raise
            except (
                aiohttp.ClientConnectionError, asyncio.TimeoutError, aiohttp.ClientPayloadError
            ):
                # connection problems, retry
                if backoff.steps > 1:
                    # just so that quick retries that sometimes happen, aren't shown
                    self.print(
                        _("error", "no_connection").format(seconds=round(delay), url=str(url))
                    )
            finally:
                if response is not None:
                    response.release()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.gui.wait_until_closed(), timeout=delay)

    @overload
    async def gql_request(self, ops: GQLOperation) -> JsonType:
        ...

    @overload
    async def gql_request(self, ops: list[GQLOperation]) -> list[JsonType]:
        ...

    async def gql_request(
        self, ops: GQLOperation | list[GQLOperation]
    ) -> JsonType | list[JsonType]:
        if gql_logger.isEnabledFor(logging.DEBUG):
            gql_logger.debug("GQL Request: %s", ops)
        backoff = ExponentialBackoff(maximum=60)
        # Use a flag to retry the request a single time, if a specific set of errors is encountered
        single_retry: bool = True
        for delay in backoff:
            async with self._qgl_limiter:
                auth_state = await self.get_auth()
                async with self.request(
                    "POST",
                    "https://gql.twitch.tv/gql",
                    json=ops,
                    headers=auth_state.headers(user_agent=self._client_type.USER_AGENT, gql=True),
                ) as response:
                    response_json: JsonType | list[JsonType] = await response.json()
            if gql_logger.isEnabledFor(logging.DEBUG):
                gql_logger.debug("GQL Response: %s", response_json)
            orig_response = response_json
            if isinstance(response_json, list):
                response_list = response_json
            else:
                response_list = [response_json]
            force_retry: bool = False
            for response_json in response_list:
                # GQL error handling
                if "errors" in response_json:
                    for error_dict in response_json["errors"]:
                        if "message" in error_dict:
                            if (
                                single_retry
                                and error_dict["message"] in (
                                    "service error",
                                    "PersistedQueryNotFound",
                                )
                            ):
                                logger.error(
                                    f"Retrying a {error_dict['message']} for "
                                    f"{response_json['extensions']['operationName']}"
                                )
                                single_retry = False
                                if delay < 5:
                                    # overwrite the delay if too short
                                    delay = 5
                                force_retry = True
                                break
                            elif error_dict["message"] == "server error":
                                # nullify the key the error path points to
                                data_dict: JsonType = response_json["data"]
                                path: list[str] = error_dict.get("path", [])
                                for key in path[:-1]:
                                    data_dict = data_dict[key]
                                data_dict[path[-1]] = None
                                break
                            elif (
                                error_dict["message"] in (
                                    "service timeout",
                                    "request cancelled",
                                    "service unavailable",
                                    "context deadline exceeded",
                                )
                            ):
                                force_retry = True
                                break
                    else:
                        errors = response_json["errors"]
                        if any(
                            e.get("message") == "PersistedQueryNotFound"
                            for e in errors
                            if isinstance(e, dict)
                        ):
                            self.print(_("error", "gql_outdated"))
                        raise GQLException(errors)
                # Other error handling
                elif "error" in response_json:
                    raise GQLException(
                        f"{response_json['error']}: {response_json['message']}"
                    )
                if force_retry:
                    break
            else:
                return orig_response
            await asyncio.sleep(delay)
        raise RuntimeError("Retry loop was broken")

    def _merge_data(self, primary_data: JsonType, secondary_data: JsonType) -> JsonType:
        merged = {}
        for key in set(chain(primary_data.keys(), secondary_data.keys())):
            in_primary = key in primary_data
            if in_primary and key in secondary_data:
                vp = primary_data[key]
                vs = secondary_data[key]
                if not isinstance(vp, type(vs)) or not isinstance(vs, type(vp)):
                    raise MinerException("Inconsistent merge data")
                if isinstance(vp, dict):  # both are dicts
                    merged[key] = self._merge_data(vp, vs)
                else:
                    # use primary value
                    merged[key] = vp
            elif in_primary:
                merged[key] = primary_data[key]
            else:  # in campaigns only
                merged[key] = secondary_data[key]
        return merged

    @staticmethod
    def _campaign_detail_sig(summary: JsonType) -> str:
        """Fingerprint of summary fields that invalidate cached CampaignDetails."""
        game = summary.get("game") or {}
        return "|".join(
            (
                str(summary.get("status") or ""),
                str(summary.get("startAt") or ""),
                str(summary.get("endAt") or ""),
                str(summary.get("name") or ""),
                str(game.get("id") if isinstance(game, dict) else ""),
            )
        )

    async def fetch_campaigns(
        self, campaigns_chunk: list[tuple[str, JsonType]]
    ) -> dict[str, JsonType]:
        campaign_ids: dict[str, JsonType] = dict(campaigns_chunk)
        auth_state = await self.get_auth()
        response_list: list[JsonType] = await self.gql_request(
            [
                GQL_QUERIES["CampaignDetails"].with_variables(
                    {"channelLogin": str(auth_state.user_id), "dropID": cid}
                )
                for cid in campaign_ids
            ]
        )
        fetched_data: dict[str, JsonType] = {
            (campaign_data := response_json["data"]["user"]["dropCampaign"])["id"]: campaign_data
            for response_json in response_list
        }
        return self._merge_data(campaign_ids, fetched_data)

    async def fetch_inventory(self) -> None:
        status_update = self.gui.status.update
        status_update(_("gui", "status", "fetching_inventory"))
        # These two queries are independent. Running them concurrently saves a
        # full network round-trip on every inventory refresh. On failure, cancel
        # the sibling too: gather alone would leave it running against a session
        # that the caller is about to close.
        inventory_task = asyncio.create_task(self.gql_request(GQL_QUERIES["Inventory"]))
        campaigns_task = asyncio.create_task(self.gql_request(GQL_QUERIES["Campaigns"]))
        try:
            inventory_response, campaigns_response = await asyncio.gather(
                inventory_task, campaigns_task
            )
        except BaseException:
            await self._cancel_tasks((inventory_task, campaigns_task))
            raise
        # fetch in-progress campaigns (inventory)
        inventory: JsonType = (
            cast(JsonType, inventory_response)["data"]["currentUser"]["inventory"]
        )
        ongoing_campaigns: list[JsonType] = inventory["dropCampaignsInProgress"] or []
        # this contains claimed benefit edge IDs, not drop IDs
        claimed_benefits: dict[str, datetime] = {
            b["id"]: timestamp(b["lastAwardedAt"]) for b in inventory["gameEventDrops"]
        }
        inventory_data: dict[str, JsonType] = {c["id"]: c for c in ongoing_campaigns}
        # general available campaigns data (campaigns)
        available_list: list[JsonType] = (
            cast(JsonType, campaigns_response)["data"]["currentUser"]["dropCampaigns"] or []
        )
        applicable_statuses = ("ACTIVE", "UPCOMING")
        available_campaigns: dict[str, JsonType] = {
            c["id"]: c
            for c in available_list
            if c["status"] in applicable_statuses  # that are currently not expired
        }
        # Reuse cached CampaignDetails when the summary fingerprint is unchanged.
        status_update(_("gui", "status", "fetching_campaigns"))
        to_fetch: list[tuple[str, JsonType]] = []
        reused = 0
        for cid, summary in available_campaigns.items():
            sig = self._campaign_detail_sig(summary)
            cached = self._campaign_detail_cache.get(cid)
            if cached is not None and self._campaign_detail_sigs.get(cid) == sig:
                inventory_data = self._merge_data(inventory_data, {cid: cached})
                reused += 1
            else:
                to_fetch.append((cid, summary))
        if reused:
            logger.log(CALL, f"Campaign details cache hit: {reused}/{len(available_campaigns)}")
        if to_fetch:
            fetch_campaigns_tasks: list[asyncio.Task[Any]] = [
                asyncio.create_task(self.fetch_campaigns(campaigns_chunk))
                for campaigns_chunk in chunk(to_fetch, 20)
            ]
            try:
                for coro in asyncio.as_completed(fetch_campaigns_tasks):
                    chunk_campaigns_data = await coro
                    # merge the inventory and campaigns datas together
                    inventory_data = self._merge_data(inventory_data, chunk_campaigns_data)
                    for cid, details in chunk_campaigns_data.items():
                        summary = available_campaigns.get(cid)
                        if summary is None:
                            continue
                        self._campaign_detail_cache[cid] = details
                        self._campaign_detail_sigs[cid] = self._campaign_detail_sig(summary)
            except BaseException:
                # asyncio.as_completed doesn't cancel tasks on errors or cancellation
                await self._cancel_tasks(fetch_campaigns_tasks)
                raise
        # Drop cache entries for campaigns that are no longer available
        stale_ids = [cid for cid in self._campaign_detail_cache if cid not in available_campaigns]
        for cid in stale_ids:
            self._campaign_detail_cache.pop(cid, None)
            self._campaign_detail_sigs.pop(cid, None)
        # filter out invalid campaigns
        for campaign_id in list(inventory_data.keys()):
            if inventory_data[campaign_id]["game"] is None:
                del inventory_data[campaign_id]

        if self.settings.dump:
            # dump the campaigns data to the dump file
            with open(DUMP_PATH, 'a', encoding="utf8") as file:
                # we need to pre-process the inventory dump a little
                dump_data: JsonType = deepcopy(inventory_data)
                for campaign_data in dump_data.values():
                    # replace ACL lists with a simple text description
                    if (
                        campaign_data["allow"]
                        and campaign_data["allow"].get("isEnabled", True)
                        and campaign_data["allow"]["channels"]
                    ):
                        # simply count the channels included in the ACL
                        campaign_data["allow"]["channels"] = (
                            f"{len(campaign_data['allow']['channels'])} channels"
                        )
                    # replace drop instance IDs, so they don't include user IDs
                    for drop_data in campaign_data["timeBasedDrops"]:
                        if "self" in drop_data and drop_data["self"]["dropInstanceID"]:
                            drop_data["self"]["dropInstanceID"] = "..."
                json.dump(dump_data, file, indent=4, sort_keys=True)
                file.write("\n\n")  # add 2x new line spacer
                json.dump(inventory["gameEventDrops"], file, indent=4, sort_keys=True, default=str)

        # use the merged data to create campaign objects
        campaigns: list[DropsCampaign] = [
            DropsCampaign(self, campaign_data, claimed_benefits)
            for campaign_data in inventory_data.values()
        ]
        campaigns.sort(key=lambda c: c.active, reverse=True)
        campaigns.sort(key=lambda c: c.upcoming and c.starts_at or c.ends_at)
        campaigns.sort(key=lambda c: c.eligible, reverse=True)

        self._drops.clear()
        self.inventory.clear()
        # NOTE: `_campaigns` is a lookup used by channel checks. It must be rebuilt
        # together with the inventory, otherwise it slowly accumulates stale campaign
        # objects across reloads and can answer questions using outdated data.
        self._campaigns.clear()
        self._mnt_triggers.clear()
        self._deadline_triggers.clear()
        # Fresh inventory: clear session skip/defer so campaigns can be retried
        self._skipped_channel_ids.clear()
        self._deferred_game_names.clear()
        switch_triggers: set[datetime] = set()
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
        # add the campaigns to the internal inventory
        for campaign in campaigns:
            self._drops.update({drop.id: drop for drop in campaign.drops})
            if campaign.can_earn_within(next_hour):
                switch_triggers.update(campaign.time_triggers)
                finish_triggers = campaign.finish_triggers()
                self._deadline_triggers.update(finish_triggers)
                switch_triggers.update(finish_triggers)
            self.inventory.append(campaign)
            self._campaigns[campaign.id] = campaign
        # Populate the inventory list. Rows are native Treeview items, so this
        # is instant and starts no image downloads; details load on selection.
        self.gui.inv.set_campaigns(campaigns)
        # Re-render the "This Week" tab now that inventory (incl. account-link
        # status) is fresh; it doesn't need CDN images.
        self.gui.weekly.refresh()
        self._mnt_triggers.extend(sorted(switch_triggers))
        # trim out all triggers that we're already past
        now = datetime.now(timezone.utc)
        while self._mnt_triggers and self._mnt_triggers[0] <= now:
            self._mnt_triggers.popleft()
        # NOTE: maintenance task is restarted at the end of each inventory fetch
        if self._mnt_task is not None and not self._mnt_task.done():
            await self._cancel_tasks([self._mnt_task])
        # Keep the user-visible schedule in sync with the task's own first period.
        self._next_reload_at = datetime.now(timezone.utc) + timedelta(hours=1)
        self._mnt_task = asyncio.create_task(self._maintenance_task())
        # Creator Badge discovery shares the inventory/reload cadence, but is
        # additionally gated to ~45 minutes so follow paging is not hammered.
        # Failures must not abort inventory: keep prior targets and continue.
        await self._refresh_creator_badge_targets_safe()

    async def _refresh_creator_badge_targets_safe(self) -> None:
        try:
            await self.fetch_creator_badge_targets()
        except Exception:
            logger.exception(
                "Creator Badge discovery failed; keeping prior targets"
            )

    def has_finishable_wanted_game(self) -> bool:
        """True if any inventory campaign for a wanted game can finish soon."""
        if not self.wanted_games:
            return False
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
        for campaign in self.inventory:
            if (
                campaign.game in self.wanted_games
                and campaign.can_earn_within(next_hour)
                and campaign.can_finish()
            ):
                return True
        return False

    def _creator_badge_mining_allowed(self) -> bool:
        return should_mine_creator_badges(
            enabled=self.settings.creator_badge_watch,
            priority=self.settings.creator_badge_priority,
            has_finishable_wanted_game=self.has_finishable_wanted_game(),
        )

    def _creator_badge_channel_ids(self) -> set[int]:
        return {event.channel_id for event in self._creator_badge_events}

    def _should_track_creator_badge_channels(self) -> bool:
        return bool(self.settings.creator_badge_watch and self._creator_badge_events)

    def _creator_badge_event_for(self, channel: Channel) -> CreatorBadgeEvent | None:
        return active_creator_badge_event_for_channel(
            self._creator_badge_events, channel_login=channel.login
        )

    def _creator_badge_can_mine_channel(self, channel: Channel) -> bool:
        if not self._creator_badge_mining_allowed():
            return False
        return self._creator_badge_event_for(channel) is not None

    def _creator_badge_status_minutes(self, event: CreatorBadgeEvent) -> int | str:
        remaining = remaining_watch_minutes(event)
        if remaining is not None:
            return remaining
        return sum(
            reward.current_minutes
            for reward in event.watch_rewards
            if not reward.is_earned
        )

    def _watching_status_text(self, channel: Channel) -> str:
        event = self._creator_badge_event_for(channel)
        if event is not None and self._creator_badge_mining_allowed():
            return _("status", "creator_badge_watching").format(
                channel=channel.name,
                minutes=self._creator_badge_status_minutes(event),
            )
        return _("status", "watching").format(channel=channel.name)

    def _drop_creator_badge_event(self, event: CreatorBadgeEvent) -> None:
        self._creator_badge_events = [
            e for e in self._creator_badge_events if e.channel_id != event.channel_id
        ]
        self._creator_badge_progress_at.pop(event.channel_id, None)

    def _leave_creator_badge_stuck(self, channel: Channel, event: CreatorBadgeEvent) -> None:
        self.print(
            _("status", "creator_badge_watching").format(
                channel=channel.name,
                minutes=self._creator_badge_status_minutes(event),
            )
            + " — stuck, skipping"
        )
        logger.log(CALL, f"Creator Badge stuck on {channel.login}; skipping")
        self._drop_creator_badge_event(event)
        self.stop_watching()
        self.request_state(State.CHANNEL_SWITCH)

    async def _update_creator_badge_progress(self, channel: Channel) -> None:
        event = self._creator_badge_event_for(channel)
        if event is None:
            return
        if not self._creator_badge_mining_allowed():
            # Finishable game appeared (fallback). Only abandon the watch if this
            # channel is not also a valid game-campaign target — dual-purpose
            # channels must keep mining the game without a thrashy stop/switch.
            if not self._can_watch_for_game_campaign(channel):
                self.stop_watching()
                self.request_state(State.CHANNEL_SWITCH)
            return
        try:
            response = await self.gql_request(
                creator_badge_owned_campaigns_query([str(channel.id)])
            )
        except Exception:
            logger.exception(
                "Creator Badge progress query failed for %s", channel.login
            )
            now = datetime.now(timezone.utc)
            last_at = self._creator_badge_progress_at.get(channel.id, now)
            if is_stuck(last_progress_at=last_at, now=now):
                self._leave_creator_badge_stuck(channel, event)
            return
        progressed = apply_creator_badge_progress(event, response)
        now = datetime.now(timezone.utc)
        if progressed:
            self._creator_badge_progress_at[channel.id] = now
            self.gui.status.update(self._watching_status_text(channel))
        if event_watch_rewards_complete(event) or not has_incomplete_watch_rewards(event):
            logger.log(
                CALL,
                f"Creator Badge complete on {channel.login}; leaving channel",
            )
            self._drop_creator_badge_event(event)
            self.stop_watching()
            self.request_state(State.CHANNEL_SWITCH)
            return
        last_at = self._creator_badge_progress_at.get(channel.id, now)
        if is_stuck(last_progress_at=last_at, now=now):
            self._leave_creator_badge_stuck(channel, event)

    async def fetch_creator_badge_targets(self, *, force: bool = False) -> list[CreatorBadgeEvent]:
        """Page follows, batch Creator Badge GQL, store incomplete watch targets."""
        if not self.settings.creator_badge_watch:
            self._creator_badge_events = []
            self._creator_badge_progress_at.clear()
            # Clear the gate so re-enable does not return [] under a stale timestamp.
            self._creator_badge_events_at = None
            return self._creator_badge_events

        now = datetime.now(timezone.utc)
        if (
            not force
            and self._creator_badge_events_at is not None
            and now - self._creator_badge_events_at < timedelta(minutes=45)
        ):
            return self._creator_badge_events

        follows = await self._page_channel_follows(limit=500)
        if not follows:
            self._creator_badge_events = []
            self._creator_badge_events_at = now
            return self._creator_badge_events

        login_by_id = {cid: login for cid, login in follows}
        # SideNav presence filter first, then full progress query for hits.
        candidate_ids = await self._creator_badge_sidenav_hits(list(login_by_id))
        if not candidate_ids:
            self._creator_badge_events = []
            self._creator_badge_events_at = now
            return self._creator_badge_events

        events: list[CreatorBadgeEvent] = []
        for id_chunk in chunk(candidate_ids, 20):
            response = await self.gql_request(
                creator_badge_owned_campaigns_query([str(cid) for cid in id_chunk])
            )
            owned = (response.get("data") or {}).get("channelOwnedDropCampaigns") or []
            for entry in owned:
                channel = entry.get("channel") or {}
                raw_id = channel.get("id")
                if raw_id is None:
                    continue
                channel_id = int(raw_id)
                login = login_by_id.get(channel_id)
                if not login:
                    continue
                # Re-wrap a single-channel payload so parse_creator_badge_event
                # can reuse the same fixture/document shape.
                single = {"data": {"channelOwnedDropCampaigns": [entry]}}
                event = parse_creator_badge_event(
                    single, channel_id=channel_id, channel_login=login
                )
                if event is not None:
                    events.append(event)

        selected = select_targets(
            events,
            exclude=self.settings.channel_exclude,
            enabled=True,
            priority=self.settings.creator_badge_priority,
            has_finishable=False,
        )
        self._creator_badge_events = selected
        self._creator_badge_events_at = now
        logger.log(
            CALL,
            f"Creator Badge targets: {len(selected)} "
            f"(from {len(candidate_ids)} sidenav hits / {len(follows)} follows)",
        )
        return self._creator_badge_events

    async def _page_channel_follows(self, *, limit: int = 500) -> list[tuple[int, str]]:
        """Paginate ChannelFollows up to ``limit`` (id, login) pairs."""
        follows: list[tuple[int, str]] = []
        cursor: str | None = None
        page_size = min(100, limit)
        while len(follows) < limit:
            variables: JsonType = {"limit": page_size, "order": "DESC"}
            if cursor:
                variables["cursor"] = cursor
            response = await self.gql_request(
                GQL_QUERIES["ChannelFollows"].with_variables(variables)
            )
            conn = ((response.get("data") or {}).get("user") or {}).get("follows") or {}
            edges = conn.get("edges") or []
            if not edges:
                break
            for edge in edges:
                node = edge.get("node") or {}
                raw_id = node.get("id")
                login = node.get("login")
                if raw_id is None or not login:
                    continue
                follows.append((int(raw_id), str(login)))
                if len(follows) >= limit:
                    break
            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            # Prefer edge cursor (Channel Points miner pattern); fall back to pageInfo.
            last_cursor = edges[-1].get("cursor") or page_info.get("endCursor")
            if not last_cursor or last_cursor == cursor:
                break
            cursor = last_cursor
        return follows[:limit]

    async def _creator_badge_sidenav_hits(self, channel_ids: list[int]) -> list[int]:
        """Return channel IDs that SideNav reports as having Creator Badge campaigns."""
        hits: list[int] = []
        for id_chunk in chunk(channel_ids, 30):
            response = await self.gql_request(
                GQL_QUERIES["SideNavCreatorBadgeDrops"].with_variables(
                    {"channelIDs": [str(cid) for cid in id_chunk]}
                )
            )
            owned = (response.get("data") or {}).get("channelOwnedDropCampaigns") or []
            for entry in owned:
                campaigns = entry.get("campaigns") or []
                if not campaigns:
                    continue
                channel = entry.get("channel") or {}
                raw_id = channel.get("id")
                if raw_id is None:
                    continue
                hits.append(int(raw_id))
        return hits

    def get_active_campaign(self, channel: Channel | None = None) -> DropsCampaign | None:
        if not self.wanted_games:
            return None
        watching_channel = self.watching_channel.get_with_default(channel)
        if watching_channel is None:
            # if we aren't watching anything, we can't earn any drops
            return None
        campaigns: list[DropsCampaign] = []
        for campaign in self.inventory:
            if campaign.can_earn(watching_channel):
                campaigns.append(campaign)
        if campaigns:
            campaigns.sort(key=lambda c: (not c.can_finish(), c.remaining_minutes))
            return campaigns[0]
        return None

    async def get_live_streams(
        self, game: Game, *, limit: int = 20, drops_enabled: bool = True
    ) -> list[Channel]:
        filters: list[str] = []
        if drops_enabled:
            filters.append("DROPS_ENABLED")
        try:
            response = await self.gql_request(
                GQL_QUERIES["GameDirectory"].with_variables({
                    "limit": limit,
                    "slug": game.slug,
                    "options": {
                        "includeRestricted": ["SUB_ONLY_LIVE"],
                        "systemFilters": filters,
                    },
                })
            )
        except GQLException as exc:
            raise MinerException(f"Game: {game.slug}") from exc
        if "game" in response["data"]:
            return [
                Channel.from_directory(
                    self, stream_channel_data["node"], drops_enabled=drops_enabled
                )
                for stream_channel_data in response["data"]["game"]["streams"]["edges"]
                if stream_channel_data["node"]["broadcaster"] is not None
            ]
        return []

    async def bulk_check_online(self, channels: abc.Iterable[Channel]):
        """
        Utilize batch GQL requests to check ONLINE status for a lot of channels at once.
        Also handles the drops_enabled check (if enabled).
        """
        acl_streams_map: dict[int, JsonType] = {}
        # Materialize once: the original iterable may be a generator and is needed
        # again for applying the results below.
        channels = list(channels)
        # Snapshot each channel's state generation. A websocket event or a
        # per-channel check can land while these bulk requests are in flight; its
        # result is newer than ours and must win.
        generations = {channel.id: channel._stream_generation for channel in channels}
        stream_gql_ops: list[GQLOperation] = [channel.stream_gql for channel in channels]
        if not stream_gql_ops:
            # shortcut for nothing to process
            # NOTE: Have to do this here, becase "channels" can be any iterable
            return
        stream_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
            asyncio.create_task(self.gql_request(stream_gql_chunk))
            for stream_gql_chunk in chunk(stream_gql_ops, 20)
        ]
        try:
            for coro in asyncio.as_completed(stream_gql_tasks):
                response_list: list[JsonType] = await coro
                for response_json in response_list:
                    channel_data: JsonType = response_json["data"]["user"]
                    if channel_data is not None:
                        acl_streams_map[int(channel_data["id"])] = channel_data
        except BaseException:
            # asyncio.as_completed doesn't cancel tasks on errors or cancellation
            await self._cancel_tasks(stream_gql_tasks)
            raise
        # for all channels with an active stream, check the available drops as well
        acl_available_drops_map: dict[int, list[JsonType]] = {}
        if self.settings.available_drops_check:
            available_gql_ops: list[GQLOperation] = [
                GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel_id)})
                for channel_id, channel_data in acl_streams_map.items()
                if channel_data["stream"] is not None  # only do this for ONLINE channels
            ]
            available_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
                asyncio.create_task(self.gql_request(available_gql_chunk))
                for available_gql_chunk in chunk(available_gql_ops, 20)
            ]
            try:
                for coro in asyncio.as_completed(available_gql_tasks):
                    response_list = await coro
                    for response_json in response_list:
                        available_info: JsonType = response_json["data"]["channel"]
                        acl_available_drops_map[int(available_info["id"])] = (
                            available_info["viewerDropCampaigns"] or []
                        )
            except BaseException:
                # asyncio.as_completed doesn't cancel tasks on errors or cancellation
                await self._cancel_tasks(available_gql_tasks)
                raise
        for channel in channels:
            channel_id = channel.id
            if channel_id not in acl_streams_map:
                continue
            if channel._stream_generation != generations[channel_id]:
                # A newer websocket event or check updated this channel while the
                # bulk query was running; do not overwrite it with older data.
                continue
            channel_data = acl_streams_map[channel_id]
            if channel_data["stream"] is None:
                # Apply the negative result instead of skipping it; otherwise a
                # missed websocket stream-down leaves the channel ONLINE forever.
                channel.set_offline()
                continue
            available_drops: list[JsonType] = acl_available_drops_map.get(channel_id, [])
            channel.external_update(channel_data, available_drops)
