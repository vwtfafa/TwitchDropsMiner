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

    @property
    def is_newer(self) -> bool:
        local = local_commit_sha()
        if not local or not self.remote_sha:
            return True
        remote = self.remote_sha.lower()
        local_l = local.lower()
        return not (remote.startswith(local_l) or local_l.startswith(remote))


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


def apply_windows_update(zip_path: Path, *, remote_sha: str = "") -> None:
    """
    Extract the Windows release zip and schedule a replace-after-exit script.

    Only supported for the packaged Windows EXE (not source / AppImage / macOS).
    After a successful copy, the batch script writes ``.update_success`` so the
    restarted app can show a confirmation dialog.
    """
    if not IS_PACKAGED or IS_APPIMAGE or sys.platform != "win32":
        raise RuntimeError("Auto-update apply is only supported for the Windows EXE build")

    extract_dir = WORKING_DIR / "_update_extract"
    if extract_dir.exists():
        # best-effort cleanup of a previous failed update
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
        # flat zip fallback
        payload = extract_dir

    bat_path = WORKING_DIR / "_apply_update.bat"
    exe_name = SELF_PATH.name
    marker = UPDATE_SUCCESS_PATH
    sha_safe = (remote_sha or "unknown").replace('"', "")
    # Use a short delay so this process can exit and release the file lock.
    bat_contents = f"""@echo off
setlocal
cd /d "{WORKING_DIR}"
echo Applying update...
timeout /t 3 /nobreak >nul
xcopy /y /e /i "{payload}\\*" "{WORKING_DIR}\\" >nul
if errorlevel 1 (
  echo Update copy failed.
  exit /b 1
)
> "{marker}" echo {sha_safe}
rmdir /s /q "{extract_dir}"
del /f /q "{zip_path}" >nul 2>&1
start "" "{WORKING_DIR}\\{exe_name}"
del /f /q "%~f0"
"""
    bat_path.write_text(bat_contents, encoding="utf-8")
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    flags = 0x00000008 | 0x00000200
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat_path)],
        cwd=str(WORKING_DIR),
        creationflags=flags,
        close_fds=True,
    )


def consume_update_success() -> str | None:
    """
    If the previous launch finished applying an update, return its SHA and clear
    the marker. Otherwise return None.
    """
    if not UPDATE_SUCCESS_PATH.exists():
        return None
    try:
        sha = UPDATE_SUCCESS_PATH.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        sha = "unknown"
    try:
        UPDATE_SUCCESS_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return sha


def can_auto_apply() -> bool:
    return bool(IS_PACKAGED and not IS_APPIMAGE and sys.platform == "win32")
