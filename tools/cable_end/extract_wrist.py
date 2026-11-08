"""Ferrule candidates from the station's wrist cameras (8 Nov rollouts): crops + the shipped model's guess, for
hand labelling. Every 4th frame of every run, both wrists.

    ../hill_climb_harness/.venv/Scripts/python.exe extract_wrist.py
"""
import collections
import glob
import json
import sys

import cv2
import numpy as np

sys.path.insert(0, '../classifiers-fix')
from harness.cable_end import LABELS, CableEndClassifier, _prep, crop, ferrules  # noqa: E402

clf = CableEndClassifier('../classifiers-fix/models/cable_end.onnx')
files = sorted(f.replace('\\', '/') for f in glob.glob('../nuc_data/frames_all/*/last-run_inputs/wrist_[lr]/*.jpg'))
by = collections.defaultdict(list)
for f in files:
    by[f.split('/')[-4]].append(f)
recs = []
for run, fs in sorted(by.items()):
    for f in fs[::4]:
        img = cv2.imread(f)
        cands = ferrules(img)[:24]
        if not cands:
            continue
        logits = clf._forward(_prep([crop(img, c) for c in cands]))
        e = np.exp(logits - logits.max(1, keepdims=True))
        p = e / e.sum(1, keepdims=True)
        for c, pp in zip(cands, p):
            recs.append({"src": f[3:], "run": run, **{k: round(float(v), 2) for k, v in c.items()},
                         "pred": LABELS[int(pp.argmax())], "p": round(float(pp.max()), 2)})
print(len(recs), "candidates from", len(by), "runs")
print(collections.Counter(r["pred"] for r in recs))
cnt = collections.Counter(r["run"] for r in recs)
print(sorted(cnt.items(), key=lambda kv: -kv[1])[:20])
json.dump(recs, open('real/wrist_cands_all.json', 'w'))
