"""Darsh's operator decisions (plans/operator_notes.yaml, from docs/decisions.pdf) applied to a plan.

A plan opts in with `operator_notes: operator_notes.yaml` (path relative to the plan file). Applied to the raw plan
mapping before validation, so the result is checked like any hand-written plan:
  - each matched stage gets `technique` (how to do it) and `watch_for` (error detection: signs it is going wrong),
    unless the plan already sets them;
  - the retry rule: a stage that may be skipped (on_fail skip) gets 2 attempts, or 3 if it is worth
    high_value_points or more. Pass-bar stages (retry_in_place) keep retrying: moving on would be a DQ;
  - extra stages from `insert_before` (e.g. push the white ball off the table before the ball stages);
  - the last cube of tower_target turned hole-up for the stick (`tower_target_last_cube`).
"""

from __future__ import annotations

import copy
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml


def load(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: operator notes must be a mapping")
    return data


def entry_for(notes: dict[str, Any], stage_name: str) -> dict[str, Any] | None:
    for entry in notes.get("stages") or []:
        if any(fnmatch(stage_name, pattern) for pattern in entry.get("match") or []):
            return entry
    return None


def attempts_for(notes: dict[str, Any], entry: dict[str, Any] | None) -> int:
    d = notes.get("decisions") or {}
    points = (entry or {}).get("points", 0) or 0
    if points >= d.get("high_value_points", 20):
        return int(d.get("high_value_attempts", 3))
    return int(d.get("attempts", 2))


def apply_raw(data: dict[str, Any], notes: dict[str, Any]) -> dict[str, Any]:
    """A new raw plan mapping with the notes applied. `data` is not modified."""
    out = copy.deepcopy(data)
    stages: list[dict[str, Any]] = []
    # New stages send new texts: only on a plan that allows free text (prompt_technique, base Dream-1). A plan
    # that must send only recorded texts (a fine-tune-ready plan) gets error detection and the retry rule only.
    inserts = (notes.get("insert_before") or {}) if out.get("prompt_technique") else {}
    names = {s.get("name") for s in out.get("stages") or []}
    for raw in out.get("stages") or []:
        for extra in inserts.get(raw.get("name"), []) or []:
            if extra.get("name") not in names:          # never twice
                stages.append(dict(extra, _inserted=True))
        stages.append(raw)
    for raw in stages:
        inserted = raw.pop("_inserted", False)
        entry = entry_for(notes, raw["name"])
        if entry:
            raw.setdefault("technique", " ".join(str(entry.get("technique", "")).split()) or None)
            raw.setdefault("watch_for", " ".join(str(entry.get("watch_for", "")).split()) or None)
            if raw.get("technique") is None:
                raw.pop("technique")
            if raw.get("watch_for") is None:
                raw.pop("watch_for")
        if not inserted and raw.get("on_fail", "retry_in_place") == "skip":
            raw["max_attempts"] = attempts_for(notes, entry)
    out["stages"] = stages
    last = notes.get("tower_target_last_cube")
    target = out.get("tower_target") or []
    cubes = [i for i, piece in enumerate(target) if "cube" in str(piece)]
    if last and cubes:
        i = cubes[-1]
        out["tower_target"] = list(target)
        out["tower_target"][i] = str(target[i]).split(",")[0].strip() + ", " + last
    return out
