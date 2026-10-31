"""The lockstep stream region: tas_stream.pack, the index, site_stream's endpoints, and stream.js's poll clock.
Mocked: no port or oracle runs; the shots are small hand-made ppm files, the clock runs in node."""

import json
import shutil
import subprocess
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

from abu_decomp import DecompError, site_core, site_stream, tas, tas_oracle, tas_stream

JS = Path(__file__).resolve().parents[1] / "src" / "abu_decomp" / "static" / "site" / "stream.js"
W, H = tas_oracle.WIDTH, tas_oracle.HEIGHT


def ppm(shade: int) -> bytes:
    return tas_oracle.PPM_HEAD + bytes([shade]) * (W * H * 3)


def make_run(root: Path, name="r1", polls=(0, 25, 50), original=None, every=25, raw="0000 0 0\n8000 5 -5\n0000 0 0\n"):
    run = root / "plays" / name
    for side, disk in (("port", "port"), ("original", "oracle")):
        for poll in original if side == "original" and original is not None else polls:
            path = run / tas.SHOTS / disk / f"poll-{poll:06d}.ppm"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(ppm(poll % 250))
    (run / "movie.raw").write_text(raw)
    (run / tas.RECORD).write_text(json.dumps({"movie": "m", "oracle": {"every": every}}))
    return run


@pytest.fixture
def server(tmp_path):
    return SimpleNamespace(cfg={"runs": tmp_path})


def test_pack_writes_both_sides_and_the_index(tmp_path):
    run = make_run(tmp_path, polls=(0, 1, 2), every=1)
    out = tas_stream.pack(run)
    index = json.loads((run / "stream" / "index.json").read_text())
    assert out == {"run": "r1", "polls": 3, "added": 6, "every": 1}
    assert index == {"run": "r1", "polls": [0, 1, 2], "inputs": [[0, 0, 0], [0x8000, 5, -5], [0, 0, 0]], "every": 1}
    for side in tas_stream.SIDES:
        assert (run / "stream" / side / "2.jpg").read_bytes()[:2] == b"\xff\xd8"


def test_a_poll_pictured_on_one_side_only_is_not_in_the_stream(tmp_path):
    run = make_run(tmp_path, polls=(0, 25, 50), original=(0, 50))
    tas_stream.pack(run)
    assert json.loads((run / "stream" / "index.json").read_text())["polls"] == [0, 50]


def test_pack_only_adds_so_pruned_shots_keep_their_frames(tmp_path):
    run = make_run(tmp_path, polls=(0, 25))
    tas_stream.pack(run)
    shutil.rmtree(run / tas.SHOTS)  # the rig prunes the ppm files
    for disk in ("port", "oracle"):
        path = run / tas.SHOTS / disk / "poll-000050.ppm"
        path.parent.mkdir(parents=True)
        path.write_bytes(ppm(9))
    out = tas_stream.pack(run)
    assert out["polls"] == 3 and out["added"] == 2
    assert json.loads((run / "stream" / "index.json").read_text())["polls"] == [0, 25, 50]


def test_inputs_are_null_past_the_movie_and_the_shot_interval_falls_back_to_the_smallest_gap(tmp_path):
    run = make_run(tmp_path, polls=(0, 40, 100), every=0, raw="0001 1 2\n")
    (run / tas.RECORD).write_text("{}")
    tas_stream.pack(run)
    index = json.loads((run / "stream" / "index.json").read_text())
    assert index["inputs"] == [[1, 1, 2], None, None] and index["every"] == 40


def test_a_run_without_pictures_refuses(tmp_path):
    run = tmp_path / "plays" / "bare"
    run.mkdir(parents=True)
    with pytest.raises(DecompError) as refused:
        tas_stream.pack(run)
    assert refused.value.key == "tas.stream"


def test_pack_all_names_runs_or_takes_every_run_with_shots(tmp_path):
    make_run(tmp_path, "a")
    make_run(tmp_path, "b")
    (tmp_path / "plays" / "none").mkdir()
    cfg = {"runs": tmp_path}
    assert [row["run"] for row in tas_stream.pack_all(cfg, [])] == ["a", "b"]
    assert [row["run"] for row in tas_stream.pack_all(cfg, ["b"])] == ["b"]
    with pytest.raises(DecompError):
        tas_stream.pack_all(cfg, ["missing"])


def test_the_index_endpoint_packs_then_answers(server, tmp_path):
    make_run(tmp_path)
    status, body = site_core.answer(server, "GET", "/api/stream/r1", "", b"")
    assert status == HTTPStatus.OK
    assert body["run"] == "r1" and body["polls"] == [0, 25, 50] and body["every"] == 25 and len(body["inputs"]) == 3


def test_an_unknown_run_or_a_run_without_a_stream_refuses_typed(server, tmp_path):
    (tmp_path / "plays" / "bare").mkdir(parents=True)
    assert site_stream.answer(server, "GET", "nope", "", b"") == (
        HTTPStatus.NOT_FOUND,
        {"kind": "error", "code": "stream.run", "run": "nope", "side": None, "poll": None},
    )
    status, body = site_stream.answer(server, "GET", "bare", "", b"")
    assert (status, body["code"]) == (HTTPStatus.NOT_FOUND, "stream.empty")
    assert site_stream.answer(server, "POST", "bare", "", b"")[0] == HTTPStatus.METHOD_NOT_ALLOWED


def test_a_frame_is_served_and_a_bad_run_side_or_poll_refuses_typed(server, tmp_path):
    run = make_run(tmp_path)
    tas_stream.pack(run)
    status, body, kind = site_stream.frame(server, "r1/original/25.jpg")
    assert (status, kind, body[:2]) == (HTTPStatus.OK, "image/jpeg", b"\xff\xd8")
    for rest, code in (
        ("zz/port/0.jpg", "stream.run"),
        ("r1/oracle/0.jpg", "stream.side"),
        ("r1/port/26.jpg", "stream.poll"),
        ("r1/port/x.jpg", "stream.poll"),
        ("r1/port", "stream.poll"),
    ):
        status, body, kind = site_stream.frame(server, rest)
        assert (status, kind, body["code"]) == (HTTPStatus.NOT_FOUND, "application/json", code), rest


def test_a_run_id_cannot_leave_the_runs_folder(server, tmp_path):
    assert site_stream.frame(server, "../x/port/0.jpg")[2] == "application/json"
    assert site_stream.answer(server, "GET", "..", "", b"")[0] == HTTPStatus.NOT_FOUND


def clock(script: str):
    """Run `script` in node with the clock functions of stream.js bound and return its JSON output."""
    node = shutil.which("node")
    assert node, "node is required to test the poll clock in stream.js"
    code = (
        f"import * as c from {json.dumps(JS.as_uri())};\n"
        f"const out = (() => {{ {script} }})();\nconsole.log(JSON.stringify(out));"
    )
    done = subprocess.run([node, "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def test_the_clock_speeds_are_one_eighth_to_sixteen_times_sixty_polls_a_second():
    assert clock("return [c.SPEEDS, c.RATE, c.SPEEDS.map(c.speedLabel)];") == [
        [0.125, 0.25, 0.5, 1, 2, 4, 8, 16],
        60,
        ["1/8x", "1/4x", "1/2x", "1x", "2x", "4x", "8x", "16x"],
    ]


def test_advance_moves_by_speed_times_sixty_polls_per_second_and_pauses_at_the_ends():
    assert clock(
        """const polls = [0, 25, 50, 75, 100], s = { pos: 0, dir: 1, speed: 1 };
        return [c.advance(polls, s, 500).pos, c.advance(polls, { ...s, speed: 16 }, 50).pos,
          c.advance(polls, { ...s, speed: 0.125 }, 1000).pos, c.advance(polls, { ...s, pos: 90 }, 1000),
          c.advance(polls, { ...s, pos: 10, dir: -1 }, 1000), c.advance(polls, { ...s, dir: 0 }, 1000).pos];"""
    ) == [
        30,
        48,
        7.5,
        {"pos": 100, "dir": 0, "speed": 1},
        {"pos": 0, "dir": 0, "speed": 1},
        0,
    ]


def test_the_shown_poll_is_the_latest_recorded_one_at_or_before_the_clock():
    polls = "const p = [10, 25, 50]; return [-5, 10, 24.9, 25, 49, 50, 99].map(x => c.indexAt(p, x));"
    assert clock(polls) == [0, 0, 0, 1, 1, 2, 2]


def test_step_seek_and_start_snap_to_recorded_polls_and_pause():
    assert clock(
        """const p = [10, 25, 50], s = { pos: 30, dir: 1, speed: 2 };
        return [c.step(p, s, 1), c.step(p, s, -1), c.step(p, { ...s, pos: 50 }, 1),
          c.step(p, { ...s, pos: 10 }, -1).pos, c.seek(p, s, 49),
          c.startPoll(p, "26"), c.startPoll(p, undefined), c.startPoll(p, "x"), c.startPoll(p, "5")];"""
    ) == [
        {"pos": 50, "dir": 0, "speed": 2},
        {"pos": 10, "dir": 0, "speed": 2},
        {"pos": 50, "dir": 0, "speed": 2},
        10,
        {"pos": 25, "dir": 0, "speed": 2},
        25,
        10,
        10,
        10,
    ]


def test_input_fields_are_typed_and_absent_inputs_stay_absent():
    assert clock("return [c.inputFields([32768, 5, -5]), c.inputFields(null)];") == [
        {"buttons": "8000", "x": 5, "y": -5},
        None,
    ]


def test_the_region_holds_no_game_names_prose_or_timers():
    source = JS.read_text() + Path(site_stream.__file__).read_text() + Path(tas_stream.__file__).read_text()
    for banned in ("setInterval", "setTimeout", "sleep(", "time.sleep"):
        assert banned not in source
