"""Try one prompt on the bot by hand: home, start Dream with the prompt, record frames + gripper readings, stop.

    python tools/try_prompt.py --label unplug_A --seconds 45 "The power strip is ... pull it straight up ..."

Writes runs/bot/tries/<stamp>_<label>/: try.json (prompt, timeline of gripper readings and station state) and
sheet.jpg (overhead + right wrist every --every seconds). The arms move: someone stays at the e-stop. Ctrl+C stops
Dream (the arms hold where they are).
"""
from __future__ import annotations

import argparse
import io
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parents[1]
BASE = "dreamlabs/dream-1-beta"


def call(station: str, route: str, body: dict | None = None, timeout: float = 15.0):
    req = urllib.request.Request(station + route, method="GET" if body is None else "POST",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data) if data[:1] in (b"{", b"[") else data


def wait_state(station: str, states: set[str], timeout: float) -> str:
    end = time.monotonic() + timeout
    st = ""
    while time.monotonic() < end:
        st = call(station, "/status")["state"]
        if st in states:
            return st
        time.sleep(0.5)
    return st


def regrip(s: str, dz: float, rec: list[dict], t0: float, lo: float, hi: float) -> float | None:
    """Dream shut the gripper on nothing: stop it, open in place, lower dz, close. The reading if it now holds."""
    call(s, "/stop", {})
    wait_state(s, {"idle"}, 15)
    call(s, "/move", {"arm": "right", "dxyz": [0.0, 0.0, 0.0], "gripper": 0.6, "duration_s": 0.8}, timeout=30)
    call(s, "/move", {"arm": "right", "dxyz": [0.0, 0.0, dz], "gripper": 0.6, "duration_s": 1.0}, timeout=30)
    call(s, "/move", {"arm": "right", "dxyz": [0.0, 0.0, 0.0], "gripper": 0.0, "duration_s": 1.0}, timeout=30)
    time.sleep(0.5)
    g = (call(s, "/arms").get("right") or {}).get("gripper") or 0.0
    held = lo < g < hi
    rec.append({"t": round(time.monotonic() - t0, 1), "state": "regrip", "chunks": None, "grip_r": round(g, 3),
                "grip_l": None, "error": None, "held": held})
    print(f"  regrip: opened, lowered {dz * 100:.1f} cm, closed -> grip {g:.3f} ({'held' if held else 'empty'})",
          flush=True)
    return g if held else None


def pull(s: str, steps: list[float], rec: list[dict], t0: float, lo: float = 0.045, seat_dz: float = 0.0,
         step_s: float = 1.5, repull: int = 0) -> dict:
    """Stop Dream (the arms hold), then lift the right hand straight up step by step with the grip squeezed."""
    call(s, "/stop", {})
    wait_state(s, {"idle"}, 15)
    if seat_dz:
        call(s, "/move", {"arm": "right", "dxyz": [0.0, 0.0, 0.0], "gripper": 0.45, "duration_s": 0.6}, timeout=30)
        call(s, "/move", {"arm": "right", "dxyz": [0.0, 0.0, -abs(seat_dz)], "gripper": 0.45, "duration_s": 1.0},
             timeout=30)
    # squeeze in place and read again: Dream's stop can loosen the jaws (8 Nov: a pull that started at 0.034 was empty)
    call(s, "/move", {"arm": "right", "dxyz": [0.0, 0.0, 0.0], "gripper": 0.0, "duration_s": 0.6}, timeout=30)
    time.sleep(0.5)
    before = call(s, "/pose")["right"]
    g = before.get("gripper") or 0.0
    if g <= lo:
        row = {"t": round(time.monotonic() - t0, 1), "state": "lost", "chunks": None, "grip_r": round(g, 3),
               "grip_l": None, "error": None}
        rec.append(row)
        print(f"  pull skipped: after the squeeze the gripper reads {g:.3f} (empty)", flush=True)
        return row
    tries = 0
    while True:
        for dz in steps:
            call(s, "/move", {"arm": "right", "dxyz": [0.0, 0.0, dz], "gripper": 0.0, "duration_s": step_s}, timeout=30)
        after = call(s, "/pose")["right"]
        if (after.get("gripper") or 0.0) > lo or tries >= repull:
            break
        tries += 1   # slipped: back down to the grip point, half open, a little lower, close, and pull again
        print(f"  slipped at z {after['xyz'][2]:.3f} (grip {after.get('gripper'):.3f}); re-grip {tries}/{repull}",
              flush=True)
        call(s, "/move", {"arm": "right", "dxyz": [0.0, 0.0, -sum(steps)], "gripper": 0.45, "duration_s": 1.5},
             timeout=30)
        call(s, "/move", {"arm": "right", "dxyz": [0.0, 0.0, -abs(seat_dz or 0.01)], "gripper": 0.45,
                          "duration_s": 0.8}, timeout=30)
        call(s, "/move", {"arm": "right", "dxyz": [0.0, 0.0, 0.0], "gripper": 0.0, "duration_s": 0.6}, timeout=30)
        time.sleep(0.4)
    row = {"t": round(time.monotonic() - t0, 1), "state": "pulled", "chunks": None,
           "grip_r": round(after.get("gripper", -1), 3), "grip_l": None, "error": None,
           "pull": {"steps_m": steps, "repulls": tries, "z_before": before["xyz"][2], "z_after": after["xyz"][2],
                    "grip_before": before.get("gripper"), "grip_after": after.get("gripper")}}
    rec.append(row)
    print(f"  pulled: z {before['xyz'][2]:.3f} -> {after['xyz'][2]:.3f} m, grip {before.get('gripper')} -> "
          f"{after.get('gripper')}", flush=True)
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--label", required=True)
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--every", type=float, default=2.0)
    ap.add_argument("--checkpoint", default=BASE)
    ap.add_argument("--wrist", default="wrist_r", help="which wrist camera goes on the sheet")
    ap.add_argument("--no-home", action="store_true", help="start from where the arms are")
    ap.add_argument("--station", default="http://127.0.0.1:8091")
    ap.add_argument("--pull-up", type=float, nargs="*", default=None, metavar="M",
                    help="grip by Dream, pull by us: once the right gripper holds something (closed, not shut), stop "
                         "Dream and lift the hand straight up by these steps (m), grip squeezed, e.g. --pull-up 0.06 0.04")
    ap.add_argument("--regrip", type=int, default=0, metavar="N",
                    help="when Dream shuts the right gripper on nothing, stop it, open in place, lower --regrip-dz, "
                         "close; held -> pull; not held -> restart Dream (at most N times)")
    ap.add_argument("--regrip-dz", type=float, default=-0.015)
    ap.add_argument("--then", default=None, metavar="PROMPT",
                    help="after the pull, run Dream with this prompt (e.g. put the cable down on the table)")
    ap.add_argument("--then-seconds", type=float, default=15.0)
    ap.add_argument("--seat-dz", type=float, default=0.0, metavar="M",
                    help="before pulling: open a little in place, lower by this much (m), close again, so the jaws bite "
                         "the plug body instead of the cord (8 Nov: plugs slipped out of the jaws on the way up)")
    ap.add_argument("--repull", type=int, default=0, metavar="N",
                    help="if the plug slips out during the pull, go straight back down to where it was gripped (no "
                         "Dream), re-grip and pull again, at most N times")
    ap.add_argument("--pull-s", type=float, default=1.5, help="seconds per pull step (slower slips less)")
    ap.add_argument("--grip-lo", type=float, default=0.045, help="below this the right gripper is shut on nothing")
    ap.add_argument("--grip-hi", type=float, default=0.20, help="above this it is not holding (8 Nov: a pull from 0.27 lost the plug)")
    a = ap.parse_args()
    out = HERE / "runs" / "bot" / "tries" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{a.label}"
    out.mkdir(parents=True)
    s = a.station
    call(s, "/stop", {})
    wait_state(s, {"idle", "error"}, 20)
    if not a.no_home:
        call(s, "/home", {})
        wait_state(s, {"idle"}, 30)
    rec, shots = [], []
    t0 = time.monotonic()
    call(s, "/start", {"prompt": a.prompt, "checkpoint": a.checkpoint})
    try:
        while (t := time.monotonic() - t0) < a.seconds:
            st = call(s, "/status")
            arms = call(s, "/arms")
            row = {"t": round(t, 1), "state": st.get("state"), "chunks": st.get("chunks"),
                   "grip_r": round((arms.get("right") or {}).get("gripper") or -1, 3),
                   "grip_l": round((arms.get("left") or {}).get("gripper") or -1, 3),
                   "error": (st.get("last_error") or {}).get("message") if st.get("state") == "error" else None}
            rec.append(row)
            imgs = [Image.open(io.BytesIO(call(s, f"/frame/{c}.jpg?w=480"))).convert("RGB") for c in ("scene", a.wrist)]
            shots.append((row, imgs))
            print(f"  {t:5.1f}s {row['state']:8} chunks {row['chunks']} grip_r {row['grip_r']} grip_l {row['grip_l']}",
                  flush=True)
            if row["state"] in ("error", "idle") and t > 5:
                break
            if a.regrip and len(rec) >= 2 and all(r["grip_r"] <= a.grip_lo for r in rec[-2:]) \
                    and any(r["grip_r"] > 0.5 for r in rec[:-2]) and row["state"] == "running":
                a.regrip -= 1
                held = regrip(s, a.regrip_dz, rec, t0, a.grip_lo, a.grip_hi)
                if held and a.pull_up:
                    rec[-1]["grip_r"] = held
                    rec.append(dict(rec[-1]))   # two holding samples: the pull below fires now
                else:
                    call(s, "/start", {"prompt": a.prompt, "checkpoint": a.checkpoint})
                    rec.append({"t": round(time.monotonic() - t0, 1), "state": "restart", "chunks": None,
                                "grip_r": 0.9, "grip_l": None, "error": None})
                    continue
            if a.pull_up and len(rec) >= 2 and all(a.grip_lo < r["grip_r"] < a.grip_hi for r in rec[-2:]) \
                    and any(r["grip_r"] > 0.5 for r in rec[:-2]):
                pulled = pull(s, a.pull_up, rec, t0, a.grip_lo, a.seat_dz, a.pull_s, a.repull)
                if pulled["state"] == "lost" and a.regrip > 0:
                    a.regrip -= 1
                    call(s, "/start", {"prompt": a.prompt, "checkpoint": a.checkpoint})
                    rec.append({"t": round(time.monotonic() - t0, 1), "state": "restart", "chunks": None,
                                "grip_r": 0.9, "grip_l": None, "error": None})
                    continue
                imgs = [Image.open(io.BytesIO(call(s, f"/frame/{c}.jpg?w=480"))).convert("RGB") for c in ("scene", "wrist_r")]
                shots.append((pulled, imgs))
                if a.then:
                    t1 = time.monotonic()
                    call(s, "/start", {"prompt": a.then, "checkpoint": a.checkpoint})
                    while time.monotonic() - t1 < a.then_seconds:
                        time.sleep(a.every)
                        arms = call(s, "/arms")
                        row = {"t": round(time.monotonic() - t0, 1), "state": "then", "chunks": None,
                               "grip_r": round((arms.get("right") or {}).get("gripper") or -1, 3), "grip_l": None,
                               "error": None}
                        rec.append(row)
                        shots.append((row, [Image.open(io.BytesIO(call(s, f"/frame/{c}.jpg?w=480"))).convert("RGB")
                                            for c in ("scene", "wrist_r")]))
                        print(f"  then {row['t']:5.1f}s grip_r {row['grip_r']}", flush=True)
                break
            time.sleep(max(0.0, a.every - (time.monotonic() - t0 - t)))
    except KeyboardInterrupt:
        pass
    finally:
        call(s, "/stop", {})
    W, H = 480, 270
    cols = 4
    rows = (len(shots) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * W, rows * H * 2), "black")
    d = ImageDraw.Draw(sheet)
    for i, (row, (scene, wr)) in enumerate(shots):
        x, y = (i % cols) * W, (i // cols) * H * 2
        sheet.paste(scene.resize((W, H)), (x, y))
        sheet.paste(wr.resize((W, H)), (x, y + H))
        d.rectangle([x, y, x + 230, y + 16], fill="black")
        d.text((x + 3, y + 2), f"{row['t']:.0f}s grip_r {row['grip_r']}", fill="yellow")
    sheet.save(out / "sheet.jpg", quality=75)
    (out / "try.json").write_text(json.dumps({"label": a.label, "prompt": a.prompt, "then": a.then,
                                               "pull_up": a.pull_up, "checkpoint": a.checkpoint,
                                               "timeline": rec}, indent=1))
    print(out)


if __name__ == "__main__":
    main()
