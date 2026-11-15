from __future__ import annotations

from updater import UpdateInfo, format_release_notes, local_commit_sha


def test_format_release_notes_strips_commit_and_truncates() -> None:
    body = (
        "Reference commit: abcdef1234567890\n\n"
        "- Fix updater restart\n"
        "- Add tray pause\n\n"
        "**Full Changelog**: https://example.com\n"
    )
    notes = format_release_notes(body)
    assert "Reference commit" not in notes
    assert "Full Changelog" not in notes
    assert "Fix updater restart" in notes
    assert "Add tray pause" in notes


def test_update_info_detects_newer_commit(monkeypatch) -> None:
    monkeypatch.setattr("updater.local_commit_sha", lambda: "aaaaaaa")
    info = UpdateInfo(
        published_at="2026-01-01T00:00:00Z",
        remote_sha="bbbbbbb",
        download_url="https://example.com/a.zip",
        asset_name="Twitch.Drops.Miner.Windows.zip",
    )
    assert info.is_newer is True


def test_update_info_same_short_sha_is_not_newer(monkeypatch) -> None:
    monkeypatch.setattr("updater.local_commit_sha", lambda: "abcdef1")
    info = UpdateInfo(
        published_at="2026-01-01T00:00:00Z",
        remote_sha="abcdef1234567890",
        download_url="https://example.com/a.zip",
        asset_name="Twitch.Drops.Miner.Windows.zip",
    )
    assert info.is_newer is False


def test_local_commit_sha_parses_dev_version(monkeypatch) -> None:
    monkeypatch.setattr("updater.__version__", "16.dev.deadbeef")
    assert local_commit_sha() == "deadbeef"
