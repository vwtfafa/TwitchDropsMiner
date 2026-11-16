from __future__ import annotations

from types import SimpleNamespace

from gui import clamp_window_geometry
from inventory import campaign_has_open_drop


def test_clamp_window_geometry_keeps_on_screen_windows() -> None:
    assert clamp_window_geometry("900x700+100+80", 1920, 1080) == "900x700+100+80"


def test_clamp_window_geometry_rejects_off_screen_and_garbage() -> None:
    assert clamp_window_geometry("900x700+4000+80", 1920, 1080) is None
    assert clamp_window_geometry("nope", 1920, 1080) is None
    assert clamp_window_geometry("100x100+10+10", 1920, 1080) is None


def test_campaign_has_open_drop() -> None:
    open_drop = SimpleNamespace(can_claim=False, can_earn=lambda: True)
    closed = SimpleNamespace(can_claim=False, can_earn=lambda: False)
    ready = SimpleNamespace(can_claim=True, can_earn=lambda: False)
    assert campaign_has_open_drop(SimpleNamespace(drops=[closed, open_drop]))
    assert campaign_has_open_drop(SimpleNamespace(drops=[ready]))
    assert not campaign_has_open_drop(SimpleNamespace(drops=[closed]))
