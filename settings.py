from __future__ import annotations

import sys
from typing import Any, TypedDict, TYPE_CHECKING, cast

from yarl import URL

from utils import json_load, json_save
from constants import SETTINGS_PATH, DEFAULT_LANG, PriorityMode

if TYPE_CHECKING:
    from main import ParsedArgs


def detect_system_dark_mode() -> bool:
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return value == 0
        except OSError:
            return False
    return False


class SettingsFile(TypedDict):
    proxy: URL
    language: str
    dark_mode: bool
    exclude: set[str]
    priority: list[str]
    browser_path: str
    autostart_tray: bool
    connection_quality: int
    tray_notifications: bool
    available_drops_check: bool
    priority_mode: PriorityMode


default_settings: SettingsFile = {
    "proxy": URL(),
    "priority": [],
    "exclude": set(),
    "browser_path": "",
    "dark_mode": detect_system_dark_mode(),
    "autostart_tray": False,
    "connection_quality": 1,
    "language": DEFAULT_LANG,
    "tray_notifications": True,
    "available_drops_check": False,
    "priority_mode": PriorityMode.ENDING_SOONEST,
}


class Settings:
    # from args
    log: bool
    tray: bool
    dump: bool
    # args properties
    debug_ws: int
    debug_gql: int
    logging_level: int
    # from settings file
    proxy: URL
    language: str
    dark_mode: bool
    exclude: set[str]
    priority: list[str]
    browser_path: str
    autostart_tray: bool
    connection_quality: int
    tray_notifications: bool
    available_drops_check: bool
    priority_mode: PriorityMode

    PASSTHROUGH = ("_settings", "_args", "_altered")

    def __init__(self, args: ParsedArgs):
        # json_load merges missing keys from defaults when merge=True (default).
        self._settings: SettingsFile = json_load(SETTINGS_PATH, default_settings)
        self._args: ParsedArgs = args
        self._altered: bool = False
        migrated = False

        # Drop obsolete keys from older settings files.
        settings_dict = cast(dict[str, Any], self._settings)
        if "enable_badges_emotes" in settings_dict:
            del settings_dict["enable_badges_emotes"]
            migrated = True

        # Ensure all keys from default_settings exist (belt-and-suspenders vs json_load merge).
        for key, value in default_settings.items():
            if key not in self._settings:
                self._settings[key] = value  # type: ignore[literal-required]
                migrated = True

        if "browser_path" not in self._settings:
            self._settings["browser_path"] = ""
            migrated = True

        # A hand-edited or old settings file can contain an unknown priority mode.
        # The settings panel also checks this, but the miner reads settings before
        # (and without) that panel being built, so validate here as the source of truth.
        if not isinstance(self._settings.get("priority_mode"), PriorityMode):
            self._settings["priority_mode"] = default_settings["priority_mode"]
            migrated = True

        if migrated:
            self._altered = True
            self.save()

    # default logic of reading settings is to check args first, then the settings file
    def __getattr__(self, name: str, /) -> Any:
        if name in self.PASSTHROUGH:
            # passthrough
            return getattr(super(), name)
        elif hasattr(self._args, name):
            return getattr(self._args, name)
        elif name in self._settings:
            return self._settings[name]  # type: ignore[literal-required]
        return getattr(super(), name)

    def __setattr__(self, name: str, value: Any, /) -> None:
        if name in self.PASSTHROUGH:
            # passthrough
            return super().__setattr__(name, value)
        elif name in self._settings:
            self._settings[name] = value  # type: ignore[literal-required]
            self._altered = True
            return
        raise TypeError(f"{name} is missing a custom setter")

    def __delattr__(self, name: str, /) -> None:
        raise RuntimeError("settings can't be deleted")

    def alter(self) -> None:
        self._altered = True

    def save(self, *, force: bool = False) -> None:
        if self._altered or force:
            json_save(SETTINGS_PATH, self._settings, sort=True)
