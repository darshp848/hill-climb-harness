"""Read-only LiveKit camera observer for Hill Climb practice and evaluation."""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import math
import os
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .log import RunLog
from .plan import Stage, load_plan
from .verifiers import MonitorVerifier

if TYPE_CHECKING:
    from livekit import rtc

# Ported from codex/hill-climb-livekit (569607e) without that branch's plan changes, so the camera-name helper
# lives here and a stage without required_cameras needs none.
_ROLE_NAMES = {
    "scene": "overhead", "top": "overhead", "overhead": "overhead",
    "left": "wrist_l", "wrist_l": "wrist_l",
    "right": "wrist_r", "wrist_r": "wrist_r",
}


def camera_matches(role: str, wanted: str) -> bool:
    """Match YAM station and simulator names, or a custom camera role substring."""
    actual = role.removeprefix("video.").removeprefix("image_window.")
    expected = wanted.removeprefix("video.").removeprefix("image_window.")
    canonical = _ROLE_NAMES.get(expected)
    return wanted in role or (canonical is not None and _ROLE_NAMES.get(actual) == canonical)


@dataclass(frozen=True)
class ReceivedFrame:
    event: rtc.VideoFrameEvent
    received_at: float

    def jpeg(self) -> bytes:
        from PIL import Image

        frame = self.event.frame
        image = Image.frombytes("RGB", (frame.width, frame.height), bytes(frame.data))
        # The SDK enumerates clockwise rotation in quarter turns.
        if self.event.rotation:
            image = image.rotate(-90 * int(self.event.rotation), expand=True)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        return buffer.getvalue()


class LiveKitCameras:
    """Keep only the latest frame for each explicitly named camera."""

    def __init__(self, publisher: str, tracks: dict[str, str]):
        from livekit import rtc

        self.room = rtc.Room()
        self.publisher = publisher
        self._roles = {name: role for role, name in tracks.items()}
        self._latest: dict[str, ReceivedFrame] = {}
        self._streams: dict[str, tuple[str, asyncio.Task]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._reconnecting = False
        self._closing = False
        self.closed = False
        self.room.on("track_published", self._subscribe)
        self.room.on("track_subscribed", self._start_stream)
        self.room.on("track_unsubscribed", self._end_stream)
        self.room.on("track_muted", self._mute)
        self.room.on("reconnecting", self._pause)
        self.room.on("reconnected", self._resume)
        self.room.on("disconnected", self._disconnected)

    async def connect(self, url: str, token: str) -> None:
        from livekit import rtc

        await self.room.connect(url, token, rtc.RoomOptions(auto_subscribe=False, connect_timeout=10))
        for participant in self.room.remote_participants.values():
            for publication in participant.track_publications.values():
                self._subscribe(publication, participant)

    def _subscribe(self, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant) -> None:
        from livekit import rtc

        if (participant.identity == self.publisher and publication.name in self._roles
                and publication.kind == rtc.TrackKind.KIND_VIDEO):
            publication.set_subscribed(True)

    def _start_stream(self, track: rtc.RemoteVideoTrack, publication: rtc.RemoteTrackPublication,
                      participant: rtc.RemoteParticipant) -> None:
        role = self._roles.get(publication.name)
        if participant.identity != self.publisher or role is None:
            return
        previous = self._streams.get(role)
        if previous is not None:
            sid, task = previous
            if sid == publication.sid and not task.done():
                return
            task.cancel()
        self._latest.pop(role, None)
        task = asyncio.create_task(self._receive(role, track, publication))
        self._streams[role] = (publication.sid, task)
        self._tasks.add(task)
        task.add_done_callback(self._stream_done)

    def _stream_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logging.getLogger(__name__).warning("LiveKit video stream failed (%s)", type(error).__name__)

    async def _receive(self, role: str, track: rtc.RemoteVideoTrack,
                       publication: rtc.RemoteTrackPublication) -> None:
        from livekit import rtc

        stream = rtc.VideoStream.from_track(track=track, capacity=1, format=rtc.VideoBufferType.RGB24)
        owner = (publication.sid, asyncio.current_task())
        try:
            async for event in stream:
                if (self._streams.get(role) == owner and not publication.muted
                        and not self._reconnecting and self.room.isconnected()):
                    self._latest[role] = ReceivedFrame(event, time.monotonic())
        finally:
            if self._streams.get(role) == owner:
                self._streams.pop(role)
                self._latest.pop(role, None)
            await stream.aclose()

    def _end_stream(self, track: rtc.RemoteTrack, publication: rtc.RemoteTrackPublication,
                    participant: rtc.RemoteParticipant) -> None:
        role = self._roles.get(publication.name)
        active = self._streams.get(role) if role is not None else None
        if active is not None and active[0] == publication.sid:
            _, task = self._streams.pop(role)
            self._latest.pop(role, None)
            task.cancel()

    def _mute(self, participant: rtc.RemoteParticipant, publication: rtc.RemoteTrackPublication) -> None:
        role = self._roles.get(publication.name)
        active = self._streams.get(role) if role is not None else None
        if active is not None and active[0] == publication.sid:
            self._latest.pop(role, None)

    def _pause(self) -> None:
        self._reconnecting = True
        self._latest.clear()

    def _resume(self) -> None:
        self._reconnecting = False

    def _disconnected(self, reason: object) -> None:
        self._latest.clear()
        self.closed = not self._closing

    def sample(self, now: float, max_age: float, previous: dict[str, ReceivedFrame]) -> dict[str, ReceivedFrame]:
        """Return a complete set only when each view is fresh and new to the caller."""
        if self._reconnecting or not self.room.isconnected() or len(self._latest) != len(self._roles):
            return {}
        if any(now - frame.received_at > max_age or frame is previous.get(role)
               for role, frame in self._latest.items()):
            return {}
        return dict(self._latest)

    async def close(self) -> None:
        self._closing = True
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.room.isconnected():
            await self.room.disconnect()


async def observe(publisher: str, tracks: dict[str, str], url: str, token: str, log: RunLog, *,
                  duration: float, interval: float, max_age: float, monitor: MonitorVerifier | None,
                  stage: Stage | None) -> tuple[int, bool]:
    cameras = LiveKitCameras(publisher, tracks)
    previous: dict[str, ReceivedFrame] = {}
    samples = 0
    try:
        await cameras.connect(url, token)
        started = next_sample = time.monotonic()
        end = started + duration
        while time.monotonic() < end and not cameras.closed:
            now = time.monotonic()
            current = cameras.sample(now, max_age, previous) if now >= next_sample else {}
            if not current:
                await asyncio.sleep(min(0.1, end - now))
                continue
            samples += 1
            previous = current
            next_sample = now + interval
            frames = {role: frame.jpeg() for role, frame in current.items()}
            files = log.save_frames("livekit", frames)
            log.event("livekit_sample", sample=samples, frames=[file.name for file in files],
                      cameras={role: {"rtc_timestamp_us": frame.event.timestamp_us,
                                      "receive_age_ms": round(1000 * (now - frame.received_at), 1)}
                               for role, frame in current.items()})
            if monitor is not None and stage is not None:
                request_started = time.monotonic()
                verdict = await asyncio.to_thread(monitor.check_now, stage, frames, now - started)
                log.event("livekit_verdict", sample=samples, stage=stage.name,
                          request_ms=round(1000 * (time.monotonic() - request_started), 1), **asdict(verdict))
        return samples, cameras.closed
    finally:
        await cameras.close()


def positive_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return number


def mint_token(key: str, secret: str, room: str, identity: str) -> str:
    """Subscribe-only token: the observer can watch the room but never publish into it."""
    from livekit import api

    grants = api.VideoGrants(room_join=True, room=room, can_subscribe=True, can_publish=False,
                             can_publish_data=False)
    return api.AccessToken(key, secret).with_identity(identity).with_grants(grants).to_jwt()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publisher", required=True, help="LiveKit participant identity that owns the cameras")
    parser.add_argument("--track", action="append", required=True, metavar="ROLE=NAME",
                        help="published video track name for overhead, wrist_l or wrist_r")
    parser.add_argument("--seconds", type=positive_seconds, default=30, help="observation window after connecting")
    parser.add_argument("--interval", type=positive_seconds, default=3, help="minimum seconds between samples")
    parser.add_argument("--max-age", type=positive_seconds, default=1, help="max seconds since local frame receipt")
    parser.add_argument("--runs-dir", default="runs_livekit")
    parser.add_argument("--plan", help="optional harness YAML for an advisory verdict")
    parser.add_argument("--stage", help="stage to assess in the observer")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--room", help="room to join when minting a token from LIVEKIT_API_KEY/SECRET")
    parser.add_argument("--identity", default="hill-climb-observer", help="our identity in a minted token")
    args = parser.parse_args(argv)
    tracks = {}
    for item in args.track:
        role, _, name = item.partition("=")
        if role not in ("overhead", "wrist_l", "wrist_r") or not name or role in tracks or name in tracks.values():
            parser.error("--track needs a unique overhead, wrist_l or wrist_r role and a unique track name")
        tracks[role] = name
    if bool(args.plan) != bool(args.stage):
        parser.error("--plan and --stage must be used together")
    url, token = os.getenv("LIVEKIT_URL"), os.getenv("LIVEKIT_TOKEN")
    key, secret = os.getenv("LIVEKIT_API_KEY"), os.getenv("LIVEKIT_API_SECRET")
    if not url or not (token or (key and secret and args.room)):
        parser.error("set LIVEKIT_URL and LIVEKIT_TOKEN (or LIVEKIT_API_KEY, LIVEKIT_API_SECRET and --room)")
    try:
        from livekit import rtc
        from PIL import Image
    except ImportError:
        parser.error("install livekit and pillow to use the observer")
    if not token:
        token = mint_token(key, secret, args.room, args.identity)
    monitor, stage = None, None
    if args.plan:
        variables = {}
        for item in args.var:
            name, _, value = item.partition("=")
            if not name or not value:
                parser.error("--var expects NAME=VALUE")
            variables[name] = value
        try:
            plan = load_plan(args.plan, variables)
            stage = next((stage for stage in plan.stages if stage.name == args.stage), None)
            if stage is None:
                parser.error(f"unknown stage {args.stage!r}")
            missing = [role for role in getattr(stage, "required_cameras", []) if not any(camera_matches(r, role) for r in tracks)]
            if missing:
                parser.error(f"stage requires camera roles {', '.join(missing)}")
            monitor = MonitorVerifier(plan, history_frames=2, timeout_s=5)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    log = RunLog(args.runs_dir)
    log.update_manifest(source="livekit", publisher=args.publisher, tracks=tracks, stage=args.stage)
    try:
        samples, disconnected = asyncio.run(observe(args.publisher, tracks, url, token, log, duration=args.seconds,
                                                    interval=args.interval, max_age=args.max_age,
                                                    monitor=monitor, stage=stage))
    except KeyboardInterrupt:
        print(f"Observer stopped. Logs at {log.path}")
        return 130
    except (rtc.ConnectError, TimeoutError) as exc:
        log.event("livekit_connect_failed", error_type=type(exc).__name__)
        print(f"LiveKit connection failed. Logs at {log.path}")
        return 1
    log.write_summary({"samples": samples, "disconnected": disconnected})
    print(f"Saved {samples} LiveKit camera sets to {log.path}")
    return 0 if samples and not disconnected else 1


if __name__ == "__main__":
    raise SystemExit(main())
