"""Fit each stage's success-vs-time curve from run logs, for harness.decide's planner.

    python tools/fit_stage_curves.py RUNS_DIR [RUNS_DIR ...] [--out plans/stage_curves.json] [--horizon 295]

Reads every segments.jsonl under the given directories. One episode = one stage in one run: the time from its
first attempt's start to the end of its first successful attempt (recoveries and retries included), or, if it
never succeeded, the total time spent on it (censored: we only know it took longer than that).

Model (harness.decide.StageModel): F(tau) = p_max * (1 - exp(-(tau - t_min) / tau_c)) for tau > t_min. Fitted by
maximum likelihood on a grid, with censoring: a success at tau adds log f(tau), a censored episode at c adds
log(1 - F(c)). Stage names are grouped by stripping trailing digits and underscores (ball_1..ball_8 -> ball).

Caveat: most logs so far are from Dream's MuJoCo YAM sim, not the Metal station. Fit station logs separately
before trusting the timeouts for a match.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.decide import StageModel, plan_timeouts  # noqa: E402

GRID_P = [i / 50 for i in range(1, 51)]
GRID_TAU = [2.0 * 1.12 ** k for k in range(45)]          # 2 s .. ~330 s


def episodes(dirs: list[Path]) -> dict[str, list[tuple[float, bool]]]:
    """stage group -> [(time, succeeded)]"""
    out: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for d in dirs:
        for seg_file in d.rglob("segments.jsonl"):
            runs: dict[str, list[dict]] = defaultdict(list)
            for line in seg_file.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("t_start") is None or r.get("t_end") is None:
                    continue
                runs[r["stage"]].append(r)
            for name, segs in runs.items():
                segs.sort(key=lambda r: r["t_start"])
                t0 = segs[0]["t_start"]
                ok = next((r for r in segs if r.get("kind") == "stage" and r.get("outcome") == "ok"), None)
                if ok is not None:
                    out[group(name)].append((ok["t_end"] - t0, True))
                else:
                    out[group(name)].append((max(r["t_end"] for r in segs) - t0, False))
    return out


def group(name: str) -> str:
    return name.rstrip("0123456789").rstrip("_") or name


def loglik(eps: list[tuple[float, bool]], p: float, tau: float, tmin: float) -> float:
    ll = 0.0
    for t, ok in eps:
        if ok:
            if t <= tmin:
                return -math.inf
            ll += math.log(p) - (t - tmin) / tau - math.log(tau)
        else:
            F = 0.0 if t <= tmin else p * (1 - math.exp(-(t - tmin) / tau))
            ll += math.log(max(1e-12, 1 - F))
    return ll


def fit(eps: list[tuple[float, bool]]) -> tuple[float, float, float, float]:
    """(p_max, tau_c, t_min, loglik)"""
    succ = [t for t, ok in eps if ok]
    tmins = [0.0] if not succ else sorted({0.0, *(round(min(succ) * f, 2) for f in (0.25, 0.5, 0.75, 0.95))})
    best = (0.0, GRID_TAU[0], 0.0, -math.inf)
    for tmin in tmins:
        for p in GRID_P:
            for tau in GRID_TAU:
                ll = loglik(eps, p, tau, tmin)
                if ll > best[3]:
                    best = (p, tau, tmin, ll)
    return best


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dirs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--min-episodes", type=int, default=5)
    a = ap.parse_args(argv)
    eps = episodes(a.dirs)
    fitted = {}
    print(f"{'stage':<24}{'n':>5}{'ok':>5}{'p_max':>8}{'tau_c s':>9}{'t_min s':>9}  median ok s")
    for name, e in sorted(eps.items()):
        if len(e) < a.min_episodes:
            continue
        p, tau, tmin, _ = fit(e)
        succ = sorted(t for t, ok in e if ok)
        med = f"{succ[len(succ) // 2]:.1f}" if succ else "-"
        print(f"{name:<24}{len(e):>5}{len(succ):>5}{p:>8.2f}{tau:>9.1f}{tmin:>9.1f}  {med}")
        fitted[name] = {"p_max": p, "tau_c": round(tau, 2), "t_min": tmin, "episodes": len(e), "successes": len(succ)}
    if a.out:
        a.out.write_text(json.dumps(fitted, indent=1) + "\n", encoding="utf-8")
        print("wrote", a.out)
    return 0


def models(curves: dict, spec: list[tuple[str, str, float, bool]]) -> list[StageModel]:
    """[(stage name, curve group, points, gate)] -> StageModels for plan_timeouts."""
    out = []
    for name, grp, value, gate in spec:
        c = curves[grp]
        out.append(StageModel(name, value, c["p_max"], c["tau_c"], c["t_min"], gate))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
