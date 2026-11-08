"""View-only LiveKit cameras for the panel: a subscriber on a thread of its own that keeps the newest JPEG per track.

It never publishes (a minted token has can_publish and can_publish_data off) and sends nothing to Dream or the
harness. `livekit` is imported on the viewer's thread only, so the panel runs as before without it, and nothing the
runner imports pulls this in.

    LIVEKIT_URL=wss://...   LIVEKIT_TOKEN=...                      # or LIVEKIT_API_KEY + LIVEKIT_API_SECRET + LIVEKIT_ROOM
    LIVEKIT_PUBLISHER=...   LIVEKIT_TRACKS=overhead=a,wrist_l=b    # both optional: default is every video track
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import threading
import time
from typing import Any

FRAME_EVERY_S = 0.5     # JPEG refresh, 2 a second
RETRY_S = 5.0
WIDTH = 480


def config_from_env(env: dict[str, str] | None = None) -> tuple[dict[str, Any] | None, str]:
    """(config, "") when the viewer can start, else (None, why it is off)."""
    env = dict(os.environ if env is None else env)
    url, token = env.get("LIVEKIT_URL"), env.get("LIVEKIT_TOKEN")
    key, secret, room = env.get("LIVEKIT_API_KEY"), env.get("LIVEKIT_API_SECRET"), env.get("LIVEKIT_ROOM")
    if not url or not (token or (key and secret and room)):
        return None, "set LIVEKIT_URL and LIVEKIT_TOKEN (or LIVEKIT_API_KEY, LIVEKIT_API_SECRET and LIVEKIT_ROOM)"
    missing = [m for m in ("livekit", "PIL") if importlib.util.find_spec(m) is None]
    if missing:
        return None, f"{' and '.join(missing)} not installed (pip install livekit==1.2.1 pillow)"
    tracks = {}
    for item in filter(None, (env.get("LIVEKIT_TRACKS") or "").split(",")):
        role, _, name = item.strip().partition("=")
        if not role or not name:
            return None, "LIVEKIT_TRACKS expects ROLE=NAME,ROLE=NAME"
        tracks[role] = name
    return {"url": url, "token": token, "key": key, "secret": secret, "room": room, "tracks": tracks,
            "publisher": env.get("LIVEKIT_PUBLISHER") or None,
            "identity": env.get("LIVEKIT_IDENTITY") or "hill-climb-panel-viewer"}, ""


def to_jpeg(frame: Any) -> bytes:
    """A ReceivedFrame as a panel-sized JPEG."""
    from PIL import Image

    image = Image.open(io.BytesIO(frame.jpeg()))
    if image.width > WIDTH:
        image = image.resize((WIDTH, round(image.height * WIDTH / image.width)))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=75)
    return out.getvalue()


class Viewer:
    """state() and jpeg(track) are all the panel reads; both are safe from any thread."""

    def __init__(self, config: dict[str, Any] | None, off_reason: str = "") -> None:
        self.config = config
        self.status = "off" if config is None else "starting"
        self.reason = off_reason
        self.room = (config or {}).get("room")
        self._frames: dict[str, tuple[bytes, float, Any]] = {}   # track -> (jpeg, received_at monotonic, source)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> Viewer:
        if self.config is not None and self._thread is None:
            self._thread = threading.Thread(target=self._main, name="livekit-viewer", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def state(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            tracks = {name: {"age_s": round(now - got, 2)} for name, (_, got, _) in sorted(self._frames.items())}
        return {"status": self.status, "reason": self.reason, "room": self.room, "tracks": tracks}

    def jpeg(self, track: str) -> bytes | None:
        with self._lock:
            item = self._frames.get(track)
        return item[0] if item else None

    def update(self, latest: dict[str, Any]) -> None:
        """Re-encode only the frames that changed since the last call; drop tracks that went away."""
        fresh = {}
        with self._lock:
            old = dict(self._frames)
        for name, frame in latest.items():
            if name in old and old[name][2] is frame:
                fresh[name] = old[name]
                continue
            try:
                fresh[name] = (to_jpeg(frame), frame.received_at, frame)
            except Exception:  # noqa: BLE001 - one bad frame must not stop the viewer
                if name in old:
                    fresh[name] = old[name]
        with self._lock:
            self._frames = fresh

    def _set(self, status: str, reason: str = "") -> None:
        self.status, self.reason = status, reason

    def _main(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        from livekit import rtc

        from .livekit_observer import LiveKitCameras, mint_token

        cfg = self.config
        assert cfg is not None
        while not self._stop.is_set():
            token = cfg["token"] or mint_token(cfg["key"], cfg["secret"], cfg["room"], cfg["identity"])
            cameras = LiveKitCameras(cfg["publisher"], cfg["tracks"])
            self._set("connecting")
            try:
                await cameras.connect(cfg["url"], token)
                self.room = cameras.room.name or self.room
                while not self._stop.is_set() and not cameras.closed:
                    self._set("reconnecting" if cameras.reconnecting else "connected",
                              "" if cameras.latest() else "connected, no video track yet")
                    self.update(cameras.latest())
                    await asyncio.sleep(FRAME_EVERY_S)
                self._set("disconnected", "room closed the connection; retrying")
            except Exception as exc:  # noqa: BLE001 - ConnectError, a bad token, a dropped socket: show it, retry
                what = "connect failed" if isinstance(exc, (rtc.ConnectError, TimeoutError)) else "viewer error"
                self._set("error", f"{what} ({type(exc).__name__}); retrying in {RETRY_S:.0f} s")
            finally:
                self.update({})
                await cameras.close()
            await asyncio.sleep(RETRY_S)


def main(argv: list[str] | None = None) -> None:
    """Standalone: just the LiveKit card on its own port, no run controls (a venv with livekit can't start a run)."""
    import argparse
    from http.server import ThreadingHTTPServer

    from . import panel

    ap = argparse.ArgumentParser(prog="python -m harness.livekit_view")
    ap.add_argument("--port", type=int, default=8098)
    a = ap.parse_args(argv)
    config, why = config_from_env()
    viewer = Viewer(config, why).start()
    handler = panel.make_handler(panel.Run(), "http://127.0.0.1:9", "", viewer)
    page = _page().encode()

    class ViewOnly(handler):  # type: ignore[misc, valid-type]
        def do_GET(self) -> None:
            if self.path == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path.startswith("/livekit/"):
                return super().do_GET()
            self._send(404, b"{}")

        def do_POST(self) -> None:
            self._send(404, b"{}")

    print(f"LiveKit viewer on http://127.0.0.1:{a.port}  ({'off: ' + why if why else 'view only'})", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", a.port), ViewOnly).serve_forever()
    except KeyboardInterrupt:
        viewer.stop()


def _page() -> str:
    from .panel import PAGE as full

    style = full[full.index("<style>"):full.index("</style>") + len("</style>")]
    card = full[full.index('<div class="card" style="margin-top:16px"><h2>LiveKit'):]
    card = card[:card.index("</div></div>") + len("</div></div>")]
    script = full[full.index("<script>\n// LiveKit viewer"):]
    script = script[:script.index("</script>") + len("</script>")]
    esc = '<script>const esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));</script>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>LiveKit Viewer</title>{style}</head>'
            f'<body><div class="wrap">{card}</div>{esc}{script}</body></html>')


if __name__ == "__main__":
    main()
