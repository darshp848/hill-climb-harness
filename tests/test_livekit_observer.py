"""LiveKit observer: the parts that run without the livekit package (freshness gate, argument checks)."""

from __future__ import annotations

import pytest

from harness import livekit_observer as lko


class _Room:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected

    def isconnected(self) -> bool:
        return self.connected


def _cameras(latest: dict, roles=("overhead", "wrist_l"), connected=True, reconnecting=False):
    cams = object.__new__(lko.LiveKitCameras)      # skip __init__: it needs the livekit SDK
    cams.room = _Room(connected)
    cams._roles = {f"track_{r}": r for r in roles}
    cams._latest = latest
    cams._reconnecting = reconnecting
    return cams


def _frame(t: float) -> lko.ReceivedFrame:
    return lko.ReceivedFrame(event=None, received_at=t)


def test_sample_needs_every_view_fresh_and_new():
    a, b = _frame(9.5), _frame(9.8)
    cams = _cameras({"overhead": a, "wrist_l": b})
    assert cams.sample(10.0, 1.0, {}) == {"overhead": a, "wrist_l": b}
    assert cams.sample(10.0, 1.0, {"overhead": a}) == {}          # one view unchanged since last sample
    assert cams.sample(11.0, 1.0, {}) == {}                       # overhead is 1.5 s old
    assert _cameras({"overhead": a}).sample(10.0, 1.0, {}) == {}  # wrist missing


def test_sample_is_empty_while_disconnected_or_reconnecting():
    latest = {"overhead": _frame(9.9), "wrist_l": _frame(9.9)}
    assert _cameras(dict(latest), connected=False).sample(10.0, 1.0, {}) == {}
    assert _cameras(dict(latest), reconnecting=True).sample(10.0, 1.0, {}) == {}


def test_camera_names_match_station_aliases():
    assert lko.camera_matches("video.top", "overhead")
    assert lko.camera_matches("left", "wrist_l")
    assert not lko.camera_matches("right", "wrist_l")


@pytest.mark.parametrize("argv, msg", [
    (["--publisher", "p", "--track", "ceiling=x"], "unique overhead"),
    (["--publisher", "p", "--track", "overhead=x", "--track", "wrist_l=x"], "unique overhead"),
    (["--publisher", "p", "--track", "overhead=x", "--plan", "plans/hill_climb_v4.yaml"], "used together"),
    (["--publisher", "p", "--track", "overhead=x"], "LIVEKIT_URL"),
])
def test_bad_arguments_are_refused(argv, msg, monkeypatch, capsys):
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    monkeypatch.delenv("LIVEKIT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        lko.main(argv)
    assert exc.value.code == 2
    assert msg in capsys.readouterr().err


def test_seconds_must_be_positive():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        lko.positive_seconds("0")
    assert lko.positive_seconds("2.5") == 2.5
