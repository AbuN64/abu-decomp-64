# SPDX-License-Identifier: GPL-3.0-or-later
"""Replay test driver: render a capture with n64c_f3d_replay and compare it with the oracle frame by SSIM.

usage: replay_ssim.py <n64c_f3d_replay> <capture-dir> <out-dir> [threshold]

capture-dir holds capture.json {"dl": "0x...", "rdram": "rdram.bin", "oracle": "oracle.png",
"fb": [w, h] (game framebuffer size, from the display list's SETCIMG width and SCISSOR; default 320x240),
"crop": [x, y, w, h] (optional, oracle pixels that show the game picture)} and the files it names.
The oracle image is box-resampled to 320x240 and compared to the render with the standard SSIM
(11x11 Gaussian window, sigma 1.5, luma). It passes only when both the whole frame and the bounding box
of non-black content reach the threshold. Needs only the standard library and numpy.
"""
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np


def read_png(path):
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat, ihdr = 8, b"", None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    w, h, depth, ctype, _, _, interlace = ihdr
    assert depth == 8 and interlace == 0 and ctype in (2, 6), f"unsupported PNG {ihdr}"
    ch = 3 if ctype == 2 else 4
    raw = zlib.decompress(idat)
    stride = w * ch
    out = np.zeros((h, stride), np.uint8)
    prev = np.zeros(stride, np.int32)
    p = 0
    for y in range(h):
        f = raw[p]
        line = np.frombuffer(raw, np.uint8, stride, p + 1).astype(np.int32)
        p += 1 + stride
        if f == 0:
            cur = line
        elif f == 1:
            cur = line.copy()
            for i in range(ch, stride):
                cur[i] = (cur[i] + cur[i - ch]) & 255
        elif f == 2:
            cur = (line + prev) & 255
        elif f == 3:
            cur = line.copy()
            for i in range(stride):
                left = cur[i - ch] if i >= ch else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 255
        else:
            cur = line.copy()
            for i in range(stride):
                a = cur[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                cur[i] = (cur[i] + pr) & 255
        out[y] = cur
        prev = cur
    return out.reshape(h, w, ch)[:, :, :3]


def read_ppm(path):
    data = Path(path).read_bytes()
    parts = data.split(b"\n", 3)
    w, h = map(int, parts[1].split())
    return np.frombuffer(parts[3], np.uint8).reshape(h, w, 3)


def box_resize(img, w, h):
    """Area-average resample (exact for integer ratios, fractional coverage otherwise)."""
    src = img.astype(np.float64)
    for axis, n in ((0, h), (1, w)):
        m = src.shape[axis]
        edges = np.linspace(0, m, n + 1)
        csum = np.concatenate([np.zeros_like(np.take(src, [0], axis)), np.cumsum(src, axis)], axis)

        def at(pos):
            i = np.minimum(np.floor(pos).astype(int), m - 1)
            frac = pos - i
            lo = np.take(csum, i, axis)
            hi = np.take(csum, np.minimum(i + 1, m), axis)
            shape = [1] * src.ndim
            shape[axis] = -1
            return lo + (hi - lo) * frac.reshape(shape)

        src = (at(edges[1:]) - at(edges[:-1])) / (m / n)
    return src


def gaussian_kernel(size=11, sigma=1.5):
    x = np.arange(size) - size // 2
    k = np.exp(-(x ** 2) / (2 * sigma ** 2))
    return k / k.sum()


def filt(img, k):
    pad = len(k) // 2
    out = np.apply_along_axis(lambda r: np.convolve(r, k, mode="valid"), 1, np.pad(img, ((0, 0), (pad, pad)), mode="reflect"))
    return np.apply_along_axis(lambda c: np.convolve(c, k, mode="valid"), 0, np.pad(out, ((pad, pad), (0, 0)), mode="reflect"))


def ssim(a, b):
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    k = gaussian_kernel()
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mu_a, mu_b = filt(a, k), filt(b, k)
    saa = filt(a * a, k) - mu_a ** 2
    sbb = filt(b * b, k) - mu_b ** 2
    sab = filt(a * b, k) - mu_a * mu_b
    s = ((2 * mu_a * mu_b + c1) * (2 * sab + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (saa + sbb + c2))
    return float(s.mean())


def luma(img):
    return img[..., 0] * 0.299 + img[..., 1] * 0.587 + img[..., 2] * 0.114


def main():
    replay, cap, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    threshold = float(sys.argv[4]) if len(sys.argv) > 4 else 0.90
    meta = json.loads((cap / "capture.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    ppm = out / "render.ppm"
    fb = meta.get("fb", [320, 240])
    subprocess.run([replay, str(cap / meta["rdram"]), meta["dl"], str(ppm), str(fb[0]), str(fb[1]), "320", "240"], check=True)
    render = read_ppm(ppm)
    oracle = read_png(cap / meta["oracle"])
    if "crop" in meta:
        x, y, w, h = meta["crop"]
        oracle = oracle[y:y + h, x:x + w]
    small = box_resize(oracle, 320, 240)
    score = ssim(luma(small), luma(render))
    (out / "oracle_320x240.ppm").write_bytes(b"P6\n320 240\n255\n" + np.clip(small + 0.5, 0, 255).astype(np.uint8).tobytes())
    # extra evidence: SSIM inside the bounding box of non-black content, and the blank-frame control
    a, b = luma(small), luma(render)
    ys, xs = np.nonzero((a > 16) | (b > 16))
    box = (slice(max(ys.min() - 5, 0), ys.max() + 6), slice(max(xs.min() - 5, 0), xs.max() + 6)) if len(ys) else (slice(None), slice(None))
    content = ssim(a[box], b[box])
    control = ssim(a, np.zeros_like(a))
    content_control = ssim(a[box], np.zeros_like(a[box]))
    print(f"SSIM {score:.4f} (threshold {threshold}); content-box {content:.4f}; blank-frame control {control:.4f} (content-box {content_control:.4f})")
    (out / "ssim.txt").write_text(f"{score:.4f}\n")
    # the whole frame is mostly black, so the gate also needs the content box to pass
    sys.exit(0 if score >= threshold and content >= threshold else 1)


if __name__ == "__main__":
    main()
