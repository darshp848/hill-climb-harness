"""Optional DimOS MCP viewer for the Hill Climb harness."""

import argparse
import base64
import json
from dataclasses import dataclass
from typing import Any

from dimos.agents.annotation import skill
from dimos.agents.mcp.mcp_server import McpServer
from dimos.core.coordination.blueprints import autoconnect
from dimos.core.coordination.module_coordinator import ModuleCoordinator
from dimos.core.module import Module, ModuleConfig

from harness.frames import FrameSource, camera_matches
from harness.station_client import StationClient


@dataclass
class WatcherSnapshot:
    status: dict[str, Any]
    images: dict[str, bytes]

    def agent_encode(self) -> list[dict[str, Any]]:
        """Encode the status and labelled JPEGs as MCP content blocks."""
        content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(self.status)}]
        for label, jpeg in self.images.items():
            content.append({"type": "text", "text": label})
            content.append({"type": "image", "mimeType": "image/jpeg",
                            "data": base64.b64encode(jpeg).decode("ascii")})
        return content


class WatcherConfig(ModuleConfig):
    station_url: str = "http://127.0.0.1:8091"
    view_url: str = "http://127.0.0.1:8093"


class HillClimbWatcher(Module):
    config: WatcherConfig

    @skill
    def inspect_watcher(self, arm: str = "left") -> WatcherSnapshot:
        """Show the overhead and selected wrist camera from the latest trip for the active prompt.

        Use both images to judge whether the wrist camera sees the cable port from the side.
        The arm argument is "left" or "right".
        """
        if arm not in ("left", "right"):
            raise ValueError('arm must be "left" or "right"')

        trip = FrameSource(self.config.view_url).latest_trip()
        station = StationClient(self.config.station_url, timeout_s=5.0).status()
        status = {k: station.get(k) for k in ("state", "prompt", "run", "chunks", "cameras")}
        status["watch_arm"] = arm
        if trip is None or station.get("state") != "running" or trip.prompt != station.get("prompt"):
            status["reason"] = "No camera trip for the active prompt yet."
            return WatcherSnapshot(status, {})

        status["trip"] = trip.n
        images = {}
        for wanted in ("overhead", arm):
            for role, jpeg in trip.frames.items():
                if camera_matches(role, wanted):
                    images["overhead" if wanted == "overhead" else f"{arm} wrist"] = jpeg
                    break
        status["views"] = list(images)
        return WatcherSnapshot(status, images)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", default="http://127.0.0.1:8091", help="Dream station API URL")
    parser.add_argument("--view", default="http://127.0.0.1:8093", help="Dream debug view URL")
    parser.add_argument("--port", type=int, default=9990, help="loopback MCP port")
    args = parser.parse_args()
    blueprint = autoconnect(
        HillClimbWatcher.blueprint(station_url=args.station, view_url=args.view),
        McpServer.blueprint(host="127.0.0.1", port=args.port),
    ).global_config(viewer="none", serve_coordinator_rpc=False)
    ModuleCoordinator.build(blueprint).loop()


if __name__ == "__main__":
    main()
