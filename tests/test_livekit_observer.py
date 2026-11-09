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


def test_minted_token_is_subscribe_only():
    jwt = pytest.importorskip("jwt")
    token = lko.mint_token("key", "secret-secret-secret-secret-1234", "station", "obs")
    claims = jwt.decode(token, "secret-secret-secret-secret-1234", algorithms=["HS256"])
    assert claims["sub"] == "obs"
    assert claims["video"]["room"] == "station"
    assert claims["video"]["canSubscribe"] and not claims["video"]["canPublish"]


# --- the panel's view-only viewer (harness/livekit_view.py + /livekit/* in harness/panel.py), no network ---

import json  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

from harness import livekit_view, panel  # noqa: E402


class _FakeViewer:
    def __init__(self) -> None:
        self.frames = {"overhead": b"\xff\xd8overhead", "pah/wrist_l": b"\xff\xd8wrist"}

    def state(self):
        return {"status": "connected", "reason": "", "room": "station",
                "tracks": {name: {"age_s": 0.2} for name in self.frames}}

    def jpeg(self, track):
        return self.frames.get(track)


@pytest.fixture
def served():
    def start(viewer):
        run = panel.Run()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), panel.make_handler(run, "http://127.0.0.1:9", "m", viewer))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}", run
    servers: list = []
    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_panel_serves_livekit_state_and_frames(served):
    base, run = served(_FakeViewer())
    status, kind, body = _get(base + "/livekit/state")
    state = json.loads(body)
    assert state["status"] == "connected" and set(state["tracks"]) == {"overhead", "pah/wrist_l"}
    assert _get(base + "/livekit/frame/overhead") == (200, "image/jpeg", b"\xff\xd8overhead")
    assert _get(base + "/livekit/frame/pah%2Fwrist_l")[2] == b"\xff\xd8wrist"
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base + "/livekit/frame/nope")
    assert exc.value.code == 404
    assert not run.running() and run.cmd == []        # viewing never starts or touches a run


def test_panel_livekit_is_get_only_and_page_has_no_livekit_inputs(served):
    base, _ = served(_FakeViewer())
    req = urllib.request.Request(base + "/livekit/state", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=3)
    assert exc.value.code == 404
    page = _get(base + "/")[2].decode()
    section = page[page.index("LiveKit (view only)"):page.index("</div></div>", page.index("LiveKit (view only)"))]
    assert "<input" not in section and "<button" not in section
    script = page[page.index("// LiveKit viewer"):]
    script = script[:script.index("</script>")]
    assert "post(" not in script and "/api/" not in script and "method" not in script


def test_panel_without_viewer_says_off(served):
    base, _ = served(None)
    assert json.loads(_get(base + "/livekit/state")[2])["status"] == "off"
    with pytest.raises(urllib.error.HTTPError):
        _get(base + "/livekit/frame/overhead")


def test_viewer_config_needs_url_and_credentials():
    config, why = livekit_view.config_from_env({})
    assert config is None and "LIVEKIT_URL" in why
    config, why = livekit_view.config_from_env({"LIVEKIT_URL": "wss://x", "LIVEKIT_API_KEY": "k"})
    assert config is None and "LIVEKIT_ROOM" in why
    viewer = livekit_view.Viewer(*livekit_view.config_from_env({})).start()
    assert viewer.state()["status"] == "off" and viewer._thread is None   # no thread, no livekit import


def test_viewer_config_reads_tracks(monkeypatch):
    monkeypatch.setattr(livekit_view.importlib.util, "find_spec", lambda name: object())
    config, why = livekit_view.config_from_env({"LIVEKIT_URL": "wss://x", "LIVEKIT_TOKEN": "t",
                                                "LIVEKIT_TRACKS": "overhead=table_top, wrist_l=left"})
    assert why == "" and config["tracks"] == {"overhead": "table_top", "wrist_l": "left"}
    assert config["publisher"] is None


class _Jpeg:
    def __init__(self, t: float, color=(200, 30, 30), size=(960, 540)) -> None:
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", size, color).save(buf, format="JPEG")
        self.data, self.received_at, self.calls = buf.getvalue(), t, 0

    def jpeg(self) -> bytes:
        self.calls += 1
        return self.data


def test_viewer_keeps_newest_frame_downscaled_and_reencodes_only_changes():
    pytest.importorskip("PIL")
    from PIL import Image
    import io
    viewer = livekit_view.Viewer({"room": "station"})
    a = _Jpeg(livekit_view.time.monotonic())
    viewer.update({"overhead": a})
    viewer.update({"overhead": a})
    assert a.calls == 1
    assert Image.open(io.BytesIO(viewer.jpeg("overhead"))).width == livekit_view.WIDTH
    assert set(viewer.state()["tracks"]) == {"overhead"}
    viewer.update({})
    assert viewer.jpeg("overhead") is None and viewer.state()["tracks"] == {}


def test_livekit_cameras_watch_every_track_when_none_are_named():
    pytest.importorskip("livekit")

    class _P:
        def __init__(self, identity, name):
            self.identity, self.name = identity, name

    cams = lko.LiveKitCameras(None, {})
    assert cams._role(_P("", "table_top"), _P("metal-station", "")) == "metal-station/table_top"
    named = lko.LiveKitCameras("metal-station", {"overhead": "table_top"})
    assert named._role(_P("", "table_top"), _P("metal-station", "")) == "overhead"
    assert named._role(_P("", "table_top"), _P("someone-else", "")) is None
    assert lko.LiveKitCameras("metal-station", {})._role(_P("", "x"), _P("metal-station", "")) == "x"


def test_standalone_viewer_page_is_the_livekit_card_only():
    page = livekit_view._page()
    assert "LiveKit (view only)" in page and "/livekit/state" in page
    assert "<input" not in page and "<button" not in page and "/api/" not in page
