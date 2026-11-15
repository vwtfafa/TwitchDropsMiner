from __future__ import annotations

import os
import re
import sys
import zipfile
import logging
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING

from version import __version__
from constants import IS_PACKAGED, IS_APPIMAGE, WORKING_DIR, SELF_PATH

if TYPE_CHECKING:
    import aiohttp

logger = logging.getLogger("TwitchDrops")

GITHUB_RELEASE_API = (
    "https://api.github.com/repos/vwtfafa/TwitchDropsMiner/releases/tags/dev-build"
)
WINDOWS_ASSET_NAME = "Twitch.Drops.Miner.Windows.zip"
COMMIT_RE = re.compile(r"Reference commit:\s*([0-9a-fA-F]+)")


@dataclass(slots=True)
class UpdateInfo:
    published_at: str
    remote_sha: str
    download_url: str
    asset_name: str
    release_notes: str = ""

    @property
    def is_newer(self) -> bool:
        local = local_commit_sha()
        if not local or not self.remote_sha:
            return True
        remote = self.remote_sha.lower()
        local_l = local.lower()
        return not (remote.startswith(local_l) or local_l.startswith(remote))


def format_release_notes(body: str, *, max_chars: int = 900) -> str:
    """Strip CI boilerplate from a GitHub release body and truncate for dialogs."""
    lines: list[str] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lower = stripped.lower()
        if lower.startswith("reference commit:"):
            continue
        if lower.startswith("**full changelog"):
            continue
        lines.append(stripped)
    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def local_commit_sha() -> str | None:
    """Extract the CI-appended short SHA from ``16.dev.<sha>`` if present."""
    parts = __version__.split(".")
    if len(parts) >= 3 and parts[0].isdigit():
        # 16.dev.abcdef1 → abcdef1
        return parts[-1]
    return None


def _pick_windows_asset(assets: list[dict]) -> dict | None:
    for asset in assets:
        name = asset.get("name") or ""
        if name == WINDOWS_ASSET_NAME:
            return asset
    for asset in assets:
        name = (asset.get("name") or "").lower()
        if name.endswith(".zip") and "windows" in name:
            return asset
    return None


async def fetch_update_info(session: aiohttp.ClientSession) -> UpdateInfo | None:
    try:
        async with session.get(
            GITHUB_RELEASE_API,
            headers={"Accept": "application/vnd.github+json"},
        ) as response:
            if response.status != 200:
                logger.warning("Update check failed: HTTP %s", response.status)
                return None
            data = await response.json()
    except Exception:
        logger.debug("Update check request failed", exc_info=True)
        return None

    published_at = data.get("published_at") or ""
    body = data.get("body") or ""
    match = COMMIT_RE.search(body)
    remote_sha = match.group(1) if match else (data.get("target_commitish") or "")
    asset = _pick_windows_asset(data.get("assets") or [])
    if asset is None:
        logger.warning("Update check: no Windows zip asset on release")
        return None
    download_url = asset.get("browser_download_url")
    if not download_url:
        return None
    return UpdateInfo(
        published_at=published_at,
        remote_sha=remote_sha,
        download_url=download_url,
        asset_name=asset.get("name") or WINDOWS_ASSET_NAME,
        release_notes=format_release_notes(body),
    )


async def download_update(
    session: aiohttp.ClientSession, info: UpdateInfo, dest_zip: Path
) -> Path:
    async with session.get(info.download_url) as response:
        response.raise_for_status()
        data = await response.read()
    dest_zip.write_bytes(data)
    return dest_zip


UPDATE_SUCCESS_PATH = WORKING_DIR / ".update_success"
UPDATE_NOTES_PATH = WORKING_DIR / ".update_notes"
UPDATE_LOG_PATH = WORKING_DIR / "_update_apply.log"


def apply_windows_update(
    zip_path: Path, *, remote_sha: str = "", release_notes: str = ""
) -> None:
    """
    Extract the Windows release zip and schedule a replace-after-exit script.

    Only supported for the packaged Windows EXE (not source / AppImage / macOS).

    Pattern (common for PyInstaller on Windows):
    1. Detached ``.bat`` waits until *this* PID exits (file locks released).
    2. Retries ``xcopy`` many times (AV / leftover locks).
    3. Writes ``.update_success``, then starts the new EXE with
       ``PYINSTALLER_RESET_ENVIRONMENT=1`` so a fresh ``_MEI`` / env is used.
    4. Always attempts restart — even if copy fails — so the user is not stranded.
    """
    if not IS_PACKAGED or IS_APPIMAGE or sys.platform != "win32":
        raise RuntimeError("Auto-update apply is only supported for the Windows EXE build")

    extract_dir = WORKING_DIR / "_update_extract"
    if extract_dir.exists():
        for root, dirs, files in os.walk(extract_dir, topdown=False):
            for name in files:
                Path(root, name).unlink(missing_ok=True)
            for name in dirs:
                Path(root, name).rmdir()
        extract_dir.rmdir()

    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    # CI packs files under "Twitch Drops Miner/"
    payload = extract_dir / "Twitch Drops Miner"
    if not payload.is_dir():
        payload = extract_dir

    install_dir = SELF_PATH.parent
    bat_path = install_dir / "_apply_update.bat"
    exe_path = SELF_PATH
    exe_name = SELF_PATH.name
    marker = UPDATE_SUCCESS_PATH
    log_path = UPDATE_LOG_PATH
    sha_safe = (remote_sha or "unknown").replace('"', "").replace("%", "")
    pid = os.getpid()

    # Paths quoted for cmd; use short relative names where we already cd'd.
    # IMPORTANT: never use `tasklist | findstr` here. In a detached/CREATE_NO_WINDOW
    # cmd, findstr can hang forever on a broken pipe/stdin (seen with PID wait).
    bat_contents = f"""@echo off
setlocal EnableExtensions
cd /d "{install_dir}"
> "{log_path}" echo update_apply start pid={pid}

REM --- wait until the old process has fully exited (releases EXE + lock.file) ---
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "try {{ Wait-Process -Id {pid} -Timeout 90 -ErrorAction Stop }} catch {{ }}"
>> "{log_path}" echo process wait finished
timeout /t 2 /nobreak >nul

REM --- retry copy: Windows/AV can hold locks a few seconds after exit ---
set /a _try=0
:retry_copy
set /a _try+=1
>> "{log_path}" echo xcopy attempt %_try%
xcopy /y /e /i /h /r "{payload}\\*" "{install_dir}\\" >> "{log_path}" 2>&1
if not errorlevel 1 goto copy_ok
if %_try% LSS 40 (
  timeout /t 1 /nobreak >nul
  goto retry_copy
)
>> "{log_path}" echo copy FAILED after %_try% attempts
goto launch

:copy_ok
>> "{log_path}" echo copy OK
> "{marker}" echo {sha_safe}
rmdir /s /q "{extract_dir}" >> "{log_path}" 2>&1
del /f /q "{zip_path}" >nul 2>&1

:launch
REM Drop stale single-instance lock if the old process died uncleanly
del /f /q "{WORKING_DIR}\\lock.file" >nul 2>&1
set PYINSTALLER_RESET_ENVIRONMENT=1
>> "{log_path}" echo starting "{exe_path}"
start "" "{exe_path}"
if errorlevel 1 (
  >> "{log_path}" echo start failed, trying by name
  start "" "{install_dir}\\{exe_name}"
)
timeout /t 1 /nobreak >nul
del /f /q "%~f0" >nul 2>&1
"""
    bat_path.write_text(bat_contents, encoding="utf-8")
    # Notes are written by Python (UTF-8); the bat only writes the success SHA.
    if release_notes.strip():
        UPDATE_NOTES_PATH.write_text(release_notes.strip(), encoding="utf-8")
    else:
        UPDATE_NOTES_PATH.unlink(missing_ok=True)
    logger.info("Update script written: %s (waiting for pid %s)", bat_path, pid)

    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    flags = 0x00000008 | 0x00000200 | 0x08000000
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat_path)],
        cwd=str(install_dir),
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def consume_update_success() -> tuple[str, str] | None:
    """
    If the previous launch finished applying an update, return ``(sha, notes)``
    and clear the markers. Otherwise return None.
    """
    if not UPDATE_SUCCESS_PATH.exists():
        return None
    try:
        sha = UPDATE_SUCCESS_PATH.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        sha = "unknown"
    notes = ""
    try:
        if UPDATE_NOTES_PATH.exists():
            notes = UPDATE_NOTES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        notes = ""
    try:
        UPDATE_SUCCESS_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        UPDATE_NOTES_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return sha, notes


def can_auto_apply() -> bool:
    return bool(IS_PACKAGED and not IS_APPIMAGE and sys.platform == "win32")
