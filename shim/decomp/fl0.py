# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute original native C and require a hash-qualified, presented frame."""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def image_metrics():
    path = Path(__file__).resolve().parents[1] / "tests/replay_ssim.py"
    spec = importlib.util.spec_from_file_location("frame_metrics", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(binary, rom, project_path, goldens, output):
    output.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    report = {"accepted": False, "scope": "native original-source FL0", "attempt": str(attempt)}
    try:
        project = json.loads(project_path.read_text())
        gate = project["frame_gate"]
        threshold = gate["threshold"]
        if not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0.90 <= threshold <= 1:
            raise ValueError("FL0 threshold must be at least0.90")
        manifest = json.loads((goldens / "manifest.json").read_text())
        checkpoint = manifest["checkpoints"][gate["checkpoint"]]
        if type(gate["retrace"]) is not int or gate["retrace"] <= 0 or checkpoint["vi"] != gate["retrace"]:
            raise ValueError("native retrace differs from oracle checkpoint")
        image = checkpoint["files"]["vi"]
        if not image:
            raise ValueError("checkpoint has no VI image")
        golden = (goldens / image["path"]).resolve()
        if not golden.is_relative_to(goldens.resolve()) or digest(golden) != image["sha256"]:
            raise ValueError("oracle image hash/path differs from manifest")
        layout = [d for d in project.get("dumps", []) if d.get("id") == project.get("version") and d.get("supported")]
        if len(layout) != 1 or digest(rom) != layout[0]["sha256"]:
            raise ValueError("user ROM hash differs from project input")
        for key in ["framebuffer", "output"]:
            if len(gate[key]) != 2 or any(type(n) is not int or not 0 < n <= 4096 for n in gate[key]):
                raise ValueError("invalid frame dimensions")
        report.update(binary_sha256=digest(binary), rom_sha256=digest(rom),
                      project_sha256=digest(project_path), golden_sha256=digest(golden),
                      checkpoint=gate["checkpoint"], retrace=gate["retrace"])
        ppm = attempt / "native.ppm"
        # The game extracts the ROM into a fresh user folder first, as on a player's first run.
        command = [str(binary.resolve()), "--no-dialog", "--user-dir", str((attempt / "user").resolve()),
                   "--rom", str(rom.resolve()), "--capture", str(ppm.resolve()), "--retrace", str(gate["retrace"]),
                   "--fb", "x".join(map(str, gate["framebuffer"])), "--window", "x".join(map(str, gate["output"]))]
        report["command"] = command
        with (attempt / "boot.log").open("w") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=False)
        report["native_exit"] = result.returncode
        if result.returncode:
            raise ValueError(f"original native boot exited {result.returncode}; no FL0 acceptance")
        log = (attempt / "boot.log").read_text()
        pattern = rf"^boot-frame: retrace={gate['retrace']} tasks=([1-9][0-9]*) commands=([1-9][0-9]*) surface=0x([0-9a-f]{{8}})$"
        if not re.search(pattern, log, re.MULTILINE) or not ppm.is_file():
            raise ValueError("native process returned without a rendered presentation")
        metrics = image_metrics()
        import numpy as np
        render = metrics.read_ppm(ppm)
        ow, oh = gate["output"]
        if render.shape != (oh, ow, 3):
            raise ValueError("native frame dimensions differ from project")
        oracle = metrics.read_png(golden)
        x, y, width, height = gate["crop"]
        if any(type(n) is not int for n in [x,y,width,height]) or x < 0 or y < 0 or width <= 0 or height <= 0 or x+width > oracle.shape[1] or y+height > oracle.shape[0]:
            raise ValueError("invalid oracle crop")
        small = metrics.box_resize(oracle[y:y+height,x:x+width], ow, oh)
        a, b = metrics.luma(small), metrics.luma(render)
        ys, xs = np.nonzero((a > 16) | (b > 16))
        if not len(ys):
            raise ValueError("both frames are blank")
        box = (slice(max(int(ys.min())-5,0),int(ys.max())+6),
               slice(max(int(xs.min())-5,0),int(xs.max())+6))
        whole = metrics.ssim(a,b)
        content = metrics.ssim(a[box],b[box])
        blank = metrics.ssim(a,np.zeros_like(a))
        report.update(whole_ssim=whole, content_ssim=content, blank_control_ssim=blank,
                      threshold=threshold, frame_sha256=digest(ppm))
        report["accepted"] = whole >= threshold and content >= threshold
        if not report["accepted"]:
            raise ValueError("native frame failed whole-frame/content-box SSIM")
    except (ValueError, OSError, KeyError, TypeError, IndexError, ImportError, AssertionError, subprocess.TimeoutExpired) as error:
        report["error"] = str(error)
        report["accepted"] = False
    (attempt / "report.json").write_text(json.dumps(report,indent=2)+"\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["binary", "rom", "project", "goldens", "output"]:
        parser.add_argument("--"+name, type=Path, required=True)
    args = parser.parse_args()
    report = run(args.binary,args.rom,args.project,args.goldens,args.output)
    print(json.dumps(report,sort_keys=True))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
