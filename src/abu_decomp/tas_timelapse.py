"""The phase timelapse (module tas_timelapse): the serve's frames in `<tas.runs>/phase/frames/<version>/` as one mp4."""

import hashlib
import resource
import subprocess
import time
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

CPU_CAP_SECONDS = 240  # the 4 CPU-minute cap, enforced on ffmpeg as RLIMIT_CPU
DEFAULT_FPS = 12


def frames(folder: Path) -> tuple[list[Path], int]:
    """(the frame files oldest first with consecutive identical bytes dropped, the number dropped)."""
    kept: list[Path] = []
    last = None
    dropped = 0
    for path in sorted(folder.glob("*.png")):
        digest = hashlib.sha1(path.read_bytes()).hexdigest()
        if digest == last:
            dropped += 1
            continue
        last = digest
        kept.append(path)
    return kept, dropped


def listing(files: list[Path], fps: int) -> str:
    """The ffmpeg concat list: each frame lasts 1/fps; the last one is named twice so its duration counts."""
    lines = []
    for path in files:
        lines += [f"file '{path.as_posix()}'", f"duration {1 / fps:.6f}"]
    lines.append(f"file '{files[-1].as_posix()}'")
    return "\n".join(lines) + "\n"


def command(work: Path, part: Path, fps: int, count: int) -> list[str]:
    scale = "scale=iw*2:ih*2:flags=neighbor,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
    return [
        "nice", "-n", "10", "ffmpeg", "-y", "-loglevel", "error", "-threads", "2",
        "-f", "concat", "-safe", "0", "-i", str(work),
        "-vf", scale, "-r", str(fps), "-frames:v", str(count),  # the repeated last file must not add a frame
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart", str(part),
    ]  # fmt: skip


def _limit() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_CAP_SECONDS, CPU_CAP_SECONDS))


def make(runs: Path, version: str, fps: int = DEFAULT_FPS) -> dict[str, Any]:
    """Write `<runs>/phase/<version>.mp4` from the frames; refuses when there are none or ffmpeg fails."""
    if fps < 1:
        raise DecompError("phase.fps", f"--fps must be at least 1, got {fps}")
    runs = runs.resolve()  # the concat list sits beside the mp4: a relative frame path would not resolve from there
    folder = runs / "phase" / "frames" / version
    files, dropped = frames(folder)
    if not files:
        raise DecompError("phase.frames", f"no frames in {folder}", "wait for tas.serve.phase_frame_minutes")
    out = runs / "phase" / f"{version}.mp4"
    work = out.with_suffix(".list")
    part = out.with_suffix(".part.mp4")
    work.write_text(listing(files, fps))
    started = time.time()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        done = subprocess.run(
            command(work, part, fps, len(files)), capture_output=True, text=True, preexec_fn=_limit, check=False
        )
    except OSError as error:
        raise DecompError("phase.ffmpeg", f"cannot run ffmpeg: {error}") from error
    finally:
        work.unlink(missing_ok=True)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if done.returncode != 0:
        part.unlink(missing_ok=True)
        raise DecompError("phase.ffmpeg", f"ffmpeg exited {done.returncode}: {done.stderr.strip()[-300:]}")
    part.replace(out)
    return {
        "version": version,
        "mp4": str(out),
        "frames": len(files),
        "skipped_identical": dropped,
        "fps": fps,
        "seconds": round(len(files) / fps, 3),
        "wall_seconds": round(time.time() - started, 2),
        "cpu_seconds": round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 2),
    }
