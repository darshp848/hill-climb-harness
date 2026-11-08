"""Train the cable-end classifier (usb_c / usb_a / none) on Blender renders + real station crops; export ONNX.

    python train.py [--epochs 12] [--real-train 0.5] [--out cable_end.onnx]

Inputs: synth/<label>/*.png (render_cable.py), real/cands/*.png + real/labels.json + real/cands.json.
Real crops are split by source video (crops of one video never sit on both sides); the high-resolution photos
(table_photos/*) are always test. 'unsure' real crops are never used. Reports per-class precision/recall on the real
test crops for a synthetic-only model and for synthetic + real-train, and checks the exported ONNX against torch
with OpenCV's DNN module (what the harness runs).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random

import cv2
import numpy as np
import torch
import torch.nn as nn
import torchvision

LABELS = ["usb_c", "usb_a", "none"]
SIZE = 96
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
HERE = os.path.dirname(os.path.abspath(__file__))


def prep(bgr: np.ndarray) -> np.ndarray:
    """BGR uint8 (any size) -> CHW float32 normalised, SIZE x SIZE. The harness does the same in numpy."""
    im = cv2.resize(bgr, (SIZE, SIZE), interpolation=cv2.INTER_AREA)[:, :, ::-1].astype(np.float32) / 255.0
    return ((im - MEAN) / STD).transpose(2, 0, 1).copy()


def augment(bgr: np.ndarray, rng: random.Random, synth: bool) -> np.ndarray:
    h, w = bgr.shape[:2]
    # zoom/shift/rotate: the runtime crop is centred on a detected ferrule but not perfectly
    z = rng.uniform(0.75, 1.35) if synth else rng.uniform(0.85, 1.15)
    ang = rng.uniform(-180, 180)
    M = cv2.getRotationMatrix2D((w / 2 + rng.uniform(-0.1, 0.1) * w, h / 2 + rng.uniform(-0.1, 0.1) * h), ang, z)
    bgr = cv2.warpAffine(bgr, M, (w, h), borderMode=cv2.BORDER_REFLECT)
    if rng.random() < 0.5:
        bgr = cv2.flip(bgr, 1)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[..., 0] = (hsv[..., 0] + rng.uniform(-4, 4)) % 180
    hsv[..., 1] *= rng.uniform(0.5, 1.3)
    hsv[..., 2] *= rng.uniform(0.6, 1.4)
    bgr = cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2BGR)
    # the wrist cameras: small, soft, compressed. Shrink to a random low res and back, blur, JPEG.
    if synth or rng.random() < 0.3:
        lo = rng.randint(24, 96)
        bgr = cv2.resize(cv2.resize(bgr, (lo, lo), interpolation=cv2.INTER_AREA), (w, h))
    if rng.random() < 0.4:
        k = rng.choice([3, 5])
        bgr = cv2.GaussianBlur(bgr, (k, k), 0)
    if rng.random() < 0.3:   # motion blur
        k = rng.randint(3, 9)
        ker = np.zeros((k, k), np.float32)
        ker[k // 2, :] = 1.0 / k
        ker = cv2.warpAffine(ker, cv2.getRotationMatrix2D((k / 2, k / 2), rng.uniform(0, 180), 1), (k, k))
        bgr = cv2.filter2D(bgr, -1, ker / max(ker.sum(), 1e-6))
    if rng.random() < 0.6:
        ok, enc = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, rng.randint(30, 90)])
        bgr = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    if rng.random() < 0.3:
        bgr = np.clip(bgr.astype(np.float32) + np.random.normal(0, rng.uniform(2, 10), bgr.shape), 0, 255).astype(np.uint8)
    return bgr


class DS(torch.utils.data.Dataset):
    def __init__(self, items, train: bool, seed=0):
        self.items, self.train = items, train
        self.rng = random.Random(seed)
        self.cache: dict[str, np.ndarray] = {}

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        path, y, synth = self.items[i]
        im = self.cache.get(path)
        if im is None:
            im = cv2.imread(path)
            if synth:
                im = cv2.resize(im, (128, 128), interpolation=cv2.INTER_AREA)
            self.cache[path] = im
        if self.train:
            im = augment(im, self.rng, synth)
        return torch.from_numpy(prep(im)), y


def real_items():
    labels = {int(k): v for k, v in json.load(open(os.path.join(HERE, "real/labels.json"))).items()}
    cands = {c["id"]: c for c in json.load(open(os.path.join(HERE, "real/cands.json")))}
    out = []
    for i, lab in labels.items():
        if lab not in LABELS:
            continue
        out.append((os.path.join(HERE, f"real/cands/{i:05d}.png"), LABELS.index(lab), cands[i]["src"]))
    return out


def synth_items():
    out = []
    for y, lab in enumerate(LABELS):
        for p in glob.glob(os.path.join(HERE, "synth", lab, "*.png")):
            out.append((p, y, True))
    return out


def model():
    m = torchvision.models.mobilenet_v3_small(weights=torchvision.models.MobileNet_V3_Small_Weights.DEFAULT)
    m.classifier[3] = nn.Linear(m.classifier[3].in_features, len(LABELS))
    return m


def train(items, epochs, dev, seed=0):
    torch.manual_seed(seed)
    m = model().to(dev)
    counts = np.bincount([y for _, y, _ in items], minlength=len(LABELS)).astype(np.float32)
    w = torch.tensor(counts.sum() / np.maximum(counts, 1) / len(LABELS), device=dev)
    dl = torch.utils.data.DataLoader(DS(items, True, seed), batch_size=128, shuffle=True, num_workers=0)
    opt = torch.optim.AdamW(m.parameters(), lr=1e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, 1e-3, total_steps=epochs * len(dl))
    lossf = nn.CrossEntropyLoss(weight=w, label_smoothing=0.05)
    for ep in range(epochs):
        m.train()
        tot = n = 0
        for x, y in dl:
            x, y = x.to(dev), y.to(dev)
            loss = lossf(m(x), y)
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
            tot += loss.item() * len(y)
            n += len(y)
        print(f"  epoch {ep + 1}/{epochs} loss {tot / n:.3f}", flush=True)
    return m.eval()


@torch.no_grad()
def predict(m, items, dev):
    ds = DS(items, False)
    xs = torch.stack([ds[i][0] for i in range(len(ds))])
    out = []
    for k in range(0, len(xs), 256):
        out.append(torch.softmax(m(xs[k:k + 256].to(dev)), 1).cpu().numpy())
    return np.concatenate(out) if out else np.zeros((0, len(LABELS)))


def report(name, probs, ys):
    pred = probs.argmax(1)
    print(f"{name}: n={len(ys)} accuracy {np.mean(pred == ys):.3f}")
    cm = np.zeros((3, 3), int)
    for t, p in zip(ys, pred):
        cm[t, p] += 1
    print("   true\\pred " + " ".join(f"{l:>6}" for l in LABELS))
    for t in range(3):
        print(f"   {LABELS[t]:>9} " + " ".join(f"{cm[t, p]:>6}" for p in range(3)))
    for k, l in enumerate(LABELS):
        tp = cm[k, k]
        prec = tp / max(cm[:, k].sum(), 1)
        rec = tp / max(cm[k].sum(), 1)
        print(f"   {l}: precision {prec:.2f} recall {rec:.2f} (n={cm[k].sum()})")
    return cm


def at_threshold(probs, ys, t):
    """What the harness does: report an end only when its class prob >= t (else nothing)."""
    pred = probs.argmax(1)
    conf = probs.max(1)
    for k, l in enumerate(LABELS[:2]):
        said = (pred == k) & (conf >= t)
        tp = int((said & (ys == k)).sum())
        print(f"   at p>={t}: {l} precision {tp / max(int(said.sum()), 1):.2f} recall {tp / max(int((ys == k).sum()), 1):.2f}"
              f" ({int(said.sum())} reported)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--real-train", type=float, default=0.5, help="share of real source videos used for training")
    ap.add_argument("--out", default=os.path.join(HERE, "cable_end.onnx"))
    ap.add_argument("--final", choices=["real", "synth+real"], default="real", help="data for the shipped model")
    ap.add_argument("--final-only", action="store_true", help="skip the held-out comparisons")
    args = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print("device", dev)
    synth = synth_items()
    real = real_items()
    print("synthetic", np.bincount([y for _, y, _ in synth], minlength=3), "real", np.bincount([y for _, y, _ in real], minlength=3))
    srcs = sorted({s for _, _, s in real if "table_photos" not in s})
    rng = random.Random(0)
    rng.shuffle(srcs)
    train_src = set(srcs[: int(len(srcs) * args.real_train)])
    real_train = [(p, y, False) for p, y, s in real if s in train_src]
    real_test = [(p, y, False) for p, y, s in real if s not in train_src]
    ys_test = np.array([y for _, y, _ in real_test])
    photo = [(p, y, False) for p, y, s in real if "table_photos" in s]
    ys_photo = np.array([y for _, y, _ in photo])
    print(f"real train {len(real_train)} test {len(real_test)} (of which photos {len(photo)})")

    def show(name, m):
        p = predict(m, real_test, dev)
        report(f"{name} on held-out real videos", p, ys_test)
        at_threshold(p, ys_test, 0.7)
        report(f"{name} on held-out photos", predict(m, photo, dev), ys_photo)

    if not args.final_only:
        print("== synthetic only")
        show("synthetic-only", train(synth, args.epochs, dev))
        print("== real only (real oversampled x8)")
        show("real-only", train(real_train * 8, args.epochs, dev))
        print("== synthetic + real (real oversampled x8)")
        show("synthetic+real", train(synth + real_train * 8, args.epochs, dev))

    # The shipped model: every labelled crop (all videos and the photos). 7 Nov, 15 epochs, held-out videos:
    # real-only 0.944 accuracy vs synthetic+real 0.918 (photos 0.73 vs 0.56), so the renders stay out by default.
    everything = [(p, y, False) for p, y, s in real]
    print(f"== final ({args.final}): all {len(everything)} real crops")
    m1 = train((synth if args.final == "synth+real" else []) + everything * 8, args.epochs, dev)
    m1 = m1.cpu().eval()
    torch.onnx.export(m1, torch.zeros(1, 3, SIZE, SIZE), args.out, input_names=["x"], output_names=["logits"],
                      opset_version=17, dynamo=False)
    net = cv2.dnn.readNetFromONNX(args.out)
    xs = np.stack([DS(real_test, False)[i][0].numpy() for i in range(min(64, len(real_test)))])
    net.setInput(xs)
    o_cv = net.forward()
    with torch.no_grad():
        o_t = m1(torch.from_numpy(xs)).numpy()
    print(f"ONNX via OpenCV vs torch: max |diff| {np.abs(o_cv - o_t).max():.2e}, argmax agree {np.mean(o_cv.argmax(1) == o_t.argmax(1)):.3f}")
    print("wrote", args.out, f"{os.path.getsize(args.out) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
