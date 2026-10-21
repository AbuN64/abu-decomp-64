"""The eyes and ears of the TAS page: what an agent that cannot watch the stream is handed instead.

`look` turns the page's recent past into small PNGs a local vision model can read: the current frame, a contact
sheet of N frames over the last P controller polls (each tile stamped with its poll number: the short video), and a
spectrogram strip of the last seconds of audio, plus per-channel RMS/peak numbers and a silence flag. Everything is
cut with ffmpeg (already the page's grabber), so the lane adds no image library.
"""

from __future__ import annotations

import array
import math
import subprocess
import wave
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

MAX_WIDTH = 1280
RATE = 48000
CHANNELS = 2
SILENT_PEAK = 0.002  # below about -54 dBFS on every channel counts as silence
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

Sample = tuple[int, bytes]  # (poll index, JPEG)


class PollCounter:
    """The number of complete lines in a recording the port is still appending to, read incrementally."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.offset = 0
        self.count = 0

    def update(self, path: Path | None) -> int:
        if path != self.path:
            self.path, self.offset, self.count = path, 0, 0
        if path is None or not path.is_file():
            return self.count
        with path.open("rb") as handle:
            handle.seek(self.offset)
            data = handle.read()
        end = data.rfind(b"\n")
        if end >= 0:
            self.count += data.count(b"\n", 0, end + 1)
            self.offset += end + 1
        return self.count


def pick(samples: list[Sample], frames: int, span: int) -> list[Sample]:
    """`frames` samples evenly spread over the last `span` polls, each the sample nearest its target poll."""
    if not samples:
        return []
    last = samples[-1][0]
    first = max(last - span, samples[0][0])
    if frames < 2 or first >= last:
        return [samples[-1]]
    targets = [first + (last - first) * i / (frames - 1) for i in range(frames)]
    return [min(samples, key=lambda s: abs(s[0] - t)) for t in targets]


def _ffmpeg(*args: str) -> None:
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True, check=False
    )
    if done.returncode:
        raise DecompError("tas.look", f"ffmpeg failed: {done.stderr.strip()[-300:]}")


def frame_png(jpeg: bytes, out: Path, work: Path) -> None:
    source = work / "frame.jpg"
    source.write_bytes(jpeg)
    _ffmpeg("-i", str(source), "-vf", f"scale='min({MAX_WIDTH},iw)':-2", "-frames:v", "1", str(out))


def contact_sheet(tiles: list[Sample], out: Path, work: Path, columns: int = 4) -> dict[str, Any]:
    """A grid of the tiles, each stamped `poll N`, at most MAX_WIDTH wide."""
    columns = max(1, min(columns, len(tiles)))
    rows = math.ceil(len(tiles) / columns)
    width = (MAX_WIDTH - 2 * (columns + 1)) // columns // 2 * 2  # padding=2 between and around
    for index, (poll, jpeg) in enumerate(tiles):
        source = work / f"tile{index:02d}.jpg"
        source.write_bytes(jpeg)
        stamp = (
            f"scale={width}:-2,drawtext=fontfile={FONT}:text='poll {poll}':x=6:y=6:fontsize=20:"
            "fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=4"
        )
        _ffmpeg("-i", str(source), "-vf", stamp, "-frames:v", "1", str(work / f"tile{index:02d}.png"))
    _ffmpeg(
        "-framerate", "1", "-i", str(work / "tile%02d.png"),
        "-vf", f"tile={columns}x{rows}:padding=2:color=black", "-frames:v", "1", str(out),
    )  # fmt: skip
    return {"tiles": len(tiles), "columns": columns, "rows": rows, "polls": [poll for poll, _ in tiles]}


def audio_stats(pcm: bytes) -> dict[str, Any]:
    """Per-channel RMS and peak (0..1 of full scale) of interleaved s16le stereo, and whether it is all silent."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // (2 * CHANNELS) * 2 * CHANNELS])
    channels = []
    for index, name in enumerate(("left", "right")):
        values = samples[index::CHANNELS]
        peak = max((abs(v) for v in values), default=0) / 32768
        rms = math.sqrt(sum(v * v for v in values) / len(values)) / 32768 if values else 0.0
        channels.append((name, round(rms, 5), round(peak, 5)))
    return {
        "seconds": round(len(samples) / CHANNELS / RATE, 2),
        "rms": {name: rms for name, rms, _ in channels},
        "peak": {name: peak for name, _, peak in channels},
        "silent": all(peak < SILENT_PEAK for _, _, peak in channels),
    }


def spectrogram(pcm: bytes, out: Path, work: Path) -> None:
    """A log-frequency spectrogram strip (time left to right) of the audio."""
    path = work / "audio.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm)
    _ffmpeg(
        "-i", str(path),
        "-lavfi", "showspectrumpic=s=1000x320:legend=0:fscale=log:color=intensity:orientation=vertical",
        "-frames:v", "1", str(out),
    )  # fmt: skip


def look(
    folder: Path, samples: list[Sample], pcm: bytes, *, frames: int, span: int, seconds: float, poll: int
) -> dict[str, Any]:
    """Write frame, sheet and spectrogram PNGs under `folder` and describe them."""
    if not samples:
        raise DecompError("tas.look", "no video frame yet: start a run first")
    folder.mkdir(parents=True, exist_ok=True)
    work = folder / "work"
    work.mkdir(exist_ok=True)
    tag = f"poll-{poll}"
    result: dict[str, Any] = {"poll": poll, "frame": str(folder / f"{tag}-frame.png")}
    frame_png(samples[-1][1], Path(result["frame"]), work)
    chosen = pick(samples, frames, span)
    result["sheet"] = str(folder / f"{tag}-sheet.png")
    result["sheet_info"] = contact_sheet(chosen, Path(result["sheet"]), work)
    keep = int(seconds * RATE) * 2 * CHANNELS
    audio = pcm[-keep:] if keep else b""
    result["audio"] = audio_stats(audio)
    if audio:
        result["spectrogram"] = str(folder / f"{tag}-audio.png")
        spectrogram(audio, Path(result["spectrogram"]), work)
    for leftover in work.iterdir():
        leftover.unlink()
    work.rmdir()
    return result
