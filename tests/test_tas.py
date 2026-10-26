import hashlib
import io
import json
import os
import signal
import subprocess
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError, tas
from abu_decomp.config import load_profile

T0 = datetime(2026, 10, 20, 19, 0, tzinfo=UTC)


class CodecTests(unittest.TestCase):
    def test_round_trip_compresses_equal_polls(self):
        polls = [(0, 0, 0)] * 3 + [(0x9000, 0, 0)] * 2 + [(0x0400, 5, -80)]
        text = tas.encode(polls, {"rom-sha1": "ab"})
        self.assertEqual(text, "# tas 1\n# rom-sha1 ab\n- *3\nA+START *2\nDOWN 5,-80\n")
        self.assertEqual(tas.decode(text), ({"tas": "1", "rom-sha1": "ab"}, polls))

    def test_raw_round_trip(self):
        polls = [(0x8000, -1, 2), (0, 0, 0)]
        self.assertEqual(tas.parse_raw("# c\n" + tas.format_raw(polls)), polls)

    def test_bad_input_names_the_line(self):
        for text, where in (
            ("# tas 1\nA+JUMP\n", "unknown button"),
            ("# tas 1\nA 1,2 3\n", "line 2"),
            ("A\n", "# tas 1"),
            ("# tas 1\nA 300,0\n", "line 2"),
        ):
            with self.assertRaises(DecompError) as caught:
                tas.decode(text)
            self.assertIn(where, str(caught.exception))
        with self.assertRaises(DecompError):
            tas.parse_raw("8000 0\n")
        with self.assertRaises(DecompError):
            tas.encode([(0x00C0, 0, 0)], {})  # bits the controller does not have

    def test_classify(self):
        self.assertEqual(tas.classify(0, "n64c-tas: done polls=9\n")["outcome"], "ok")
        self.assertEqual(
            tas.classify(125, "debug-reason: host-fault-00002b88\n"),
            {"outcome": "crash", "subject": "host-fault-00002b88"},
        )
        self.assertEqual(
            tas.classify(125, "debug-reason: func_80001234\n"), {"outcome": "stop", "subject": "func_80001234"}
        )
        self.assertEqual(tas.classify(139, "debug-signal: SIGSEGV\n"), {"outcome": "crash", "subject": "SIGSEGV"})
        self.assertEqual(
            tas.classify(78, "x\nboot-missing: code-pointer-8011fe88\n"),
            {"outcome": "stop", "subject": "code-pointer-8011fe88"},
        )
        miss = "n64c-fault: TLB-miss-00118e38 load at host pc 0x81a8af6\n"
        wild = miss + "boot-shim: osSetIntMask f\nboot-missing: rw_boot_entry\n"
        crash = tas.classify(78, wild)
        self.assertEqual((crash["outcome"], crash["subject"]), ("crash", "host-fault-00118e38"))
        self.assertIn("0x81a8af6", crash["fault"])
        mapped = (
            "n64c-fault: TLB-miss-00118e38 load at host pc 0x81a8af6\nboot-step: 3/9 f\nboot-missing: rw_boot_entry\n"
        )
        self.assertEqual(tas.classify(78, mapped), {"outcome": "stop", "subject": "rw_boot_entry"})
        self.assertEqual(tas.classify(None, ""), {"outcome": "hang"})
        self.assertEqual(tas.classify(1, "boom"), {"outcome": "exit", "subject": "exit 1"})


def _fake_wait(process, timeout, run=None):
    """tas._wait over a mocked Popen: its wait() stands in for wait4; the CPU reads 1.5 s."""
    try:
        return process.wait(timeout), 1.5
    except subprocess.TimeoutExpired:
        tas.os.killpg(process.pid, 9)
        process.wait()
        return None, 1.5


class MovieTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "game.z64").write_bytes(b"rom")
        (self.root / "port").write_text("")
        seat = self.root / "seat.json"
        seat.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "t",
                    "tas": {
                        "movies": "movies",
                        "runs": "runs",
                        "port": "port",
                        "rom": "game.z64",
                        "args": ["--user-dir", "{user_dir}"],
                        "exit_after": 7,
                        "timeout_seconds": 5,
                    },
                }
            )
        )
        self.profile = load_profile(seat)
        patch = mock.patch.object(tas, "_wait", _fake_wait)
        patch.start()
        self.addCleanup(patch.stop)
        self.live = self.root / "runs" / "live"
        self.live.mkdir(parents=True)

    def test_save_newest_recording_then_list_show_delete(self):
        (self.live / "20261020T180000Z.raw").write_text("0000 0 0\n")
        (self.live / "20261020T181000Z.raw").write_text("0000 0 0\n0000 0 0\n8000 0 0\n")
        saved = tas.save(self.profile, "press-a", source=None, note="press  A", replace=False, now=T0)
        self.assertEqual(saved["polls"], 3)
        self.assertTrue(saved["source"].endswith("181000Z.raw"))
        header, polls = tas.decode((self.root / "movies" / "press-a.tas").read_text())
        self.assertEqual(header["rom-sha1"], hashlib.sha1(b"rom").hexdigest())
        self.assertEqual(header["note"], "press A")
        self.assertEqual(polls, [(0, 0, 0), (0, 0, 0), (0x8000, 0, 0)])
        with self.assertRaises(DecompError):
            tas.save(self.profile, "press-a", source=None, note="", replace=False)
        with self.assertRaises(DecompError):
            tas.save(self.profile, "Press A", source=None, note="", replace=False)
        rows = tas.listing(self.profile)
        self.assertEqual([m["name"] for m in rows["movies"]], ["press-a"])
        self.assertEqual(len(rows["recordings"]), 2)
        self.assertEqual(tas.show(self.profile, "press-a")["polls"], 3)
        tas.delete(self.profile, "press-a")
        self.assertEqual(tas.listing(self.profile)["movies"], [])

    def test_play_refuses_another_rom(self):
        (self.root / "movies").mkdir()
        (self.root / "movies" / "m.tas").write_text("# tas 1\n# rom-sha1 0000\nA\n")
        with self.assertRaises(DecompError) as caught:
            tas.play(self.profile, "m")
        self.assertIn("recorded on ROM", str(caught.exception))

    def test_play_runs_the_port_under_the_bridge_and_reports_the_crash(self):
        (self.root / "movies").mkdir()
        sha = hashlib.sha1(b"rom").hexdigest()
        (self.root / "movies" / "m.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\nA *2\n")
        seen = {}

        class Port:
            def __init__(self, argv, env, stdout, **kwargs):
                seen.update(argv=argv, env=env)
                run = Path(argv[argv.index("--run-dir") + 1])
                self.assert_movie = (run / "movie.raw").read_text()
                Path(env["N64C_TAS_RECORD"]).write_text("8000 0 0\n")
                (run / "failure-stack.txt").write_text("#0 game\n")
                stdout.write("debug-reason: host-fault-00002b88\n")
                stdout.flush()
                self.pid = 1
                self.stdin = io.StringIO()

            def wait(self, timeout=None):
                return 125

        with mock.patch.object(tas.subprocess, "Popen", Port):
            result = tas.play(self.profile, "m", now=T0)
        self.assertEqual(result["outcome"], "crash")
        self.assertEqual(result["subject"], "host-fault-00002b88")
        self.assertEqual((result["polls"], result["polls_reached"]), (2, 1))
        self.assertTrue(result["stacks"][0].endswith("failure-stack.txt"))
        self.assertEqual(seen["env"]["N64C_TAS_EXIT_AFTER"], "7")
        self.assertIn(tas.STOP_FUNCTION, seen["argv"])
        user = seen["argv"][-1]
        self.assertTrue(user.endswith("/user") and Path(user).is_dir())

    def test_a_crash_is_a_finding_of_its_batch_and_a_clean_replay_clears_it(self):
        from abu_decomp import tas_buckets, tas_findings

        (self.root / "movies").mkdir()
        sha = hashlib.sha1(b"rom").hexdigest()
        (self.root / "movies" / "m.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\nA *2\n")
        outcome = {"code": 125, "text": "debug-reason: host-fault-00000010\n"}

        class Port:
            def __init__(self, argv, env, stdout, **kwargs):
                Path(env["N64C_TAS_RECORD"]).write_text("8000 0 0\n")
                stdout.write(outcome["text"])
                stdout.flush()
                self.pid, self.stdin = 1, io.StringIO()

            def wait(self, timeout=None):
                return outcome["code"]

        runs = self.root / "runs"
        with mock.patch.object(tas.subprocess, "Popen", Port):
            tas.play(self.profile, "m", now=T0, batch="b1")
            (found,) = tas_findings.read(runs)
            self.assertEqual((found["kind"], found["movie"], found["poll"], found["batch"]), ("crash", "m", 1, "b1"))
            self.assertEqual(found["subject"], "host-fault-00000010")
            self.assertEqual(len(tas_buckets.build(tas_findings.read(runs))), 1)
            outcome.update(code=0, text="n64c-tas: done\n")
            tas.play(self.profile, "m", now=T0 + timedelta(seconds=9))
        self.assertEqual([r["kind"] for r in tas_findings.read(runs)], ["crash", "clear"])
        self.assertEqual(tas_buckets.build(tas_findings.read(runs)), [])

    def test_play_kills_a_hang(self):
        (self.root / "movies").mkdir()
        sha = hashlib.sha1(b"rom").hexdigest()
        (self.root / "movies" / "m.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\n-\n")

        class Port:
            pid = 4242
            stdin = io.StringIO()

            def __init__(self, *args, **kwargs):
                self.calls = 0

            def wait(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("port", timeout)
                return -9

        with (
            mock.patch.object(tas.subprocess, "Popen", Port),
            mock.patch.object(tas.os, "killpg") as kill,
        ):
            result = tas.play(self.profile, "m", now=T0)
        kill.assert_called_once()
        self.assertEqual(result["outcome"], "hang")

    def movie(self, text="A *2\n"):
        (self.root / "movies").mkdir(exist_ok=True)
        sha = hashlib.sha1(b"rom").hexdigest()
        (self.root / "movies" / "m.tas").write_text(f"# tas 1\n# rom-sha1 {sha}\n{text}")

    def port(self, hashes, seen):
        """A port stand-in: writes the hash lines it is given and its picture, then ends cleanly."""
        seen_argv = self.argv = []

        class Port:
            pid = 1
            stdin = io.StringIO()

            def __init__(self, argv, env, stdout, **kwargs):
                self.returncode = None
                seen.append(env)
                seen_argv.append(argv)
                run = (
                    Path(argv[argv.index("--run-dir") + 1])
                    if "--run-dir" in argv
                    else Path(env["N64C_TAS_HASH"]).parent
                )
                seen.append(json.loads((run / "run.json").read_text()))
                Path(env["N64C_TAS_HASH"]).write_text(hashes)
                Path(env["N64C_VIEW"]).write_bytes(b"BM")
                Path(env["N64C_TAS_RECORD"]).write_text("8000 0 0\n8000 0 0\n")
                stdout.write("n64c-tas: done polls=2\n")

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

        return mock.patch.object(tas.subprocess, "Popen", Port)

    def test_inspection_options_set_the_ports_variables_and_list_what_it_wrote(self):
        self.movie()
        seen = []
        with self.port("1 aa\n2 bb\n", seen):
            result = tas.play(self.profile, "m", now=T0, shots_every=30, dump_ram_at=40, trace_draws=50)
        env = seen[0]
        run = Path(result["run"])
        self.assertEqual((env["N64C_TAS_SHOT_EVERY"], env["N64C_TAS_SHOT_DIR"]), ("30", str(run / "shots")))
        self.assertEqual((env["N64C_TAS_DUMP_AT"], env["N64C_TAS_DUMP_PATH"]), ("40", str(run / "ram-40.bin")))
        self.assertEqual(env["N64C_TAS_TRACE_DRAWS"], f"50:{run / 'draws-50.txt'}")
        self.assertNotIn("inspect", result)  # the stand-in port wrote none of them
        (run / "shots" / "poll-000030.ppm").write_bytes(b"P6")
        (run / "draws-50.txt").write_text("draw 0\n")
        self.assertEqual(
            tas.inspect_files(run, 30, 40, 50), [str(run / "shots" / "poll-000030.ppm"), str(run / "draws-50.txt")]
        )
        with self.assertRaises(DecompError):
            tas.check_inspect(-1, None, None)

    def test_a_first_pass_runs_the_port_without_the_bridge(self):
        self.movie()
        with self.port("1 aa\n", []):
            result = tas.play(self.profile, "m", now=T0, supervise=False, first_pass=True)
        self.assertEqual(result["outcome"], "ok")
        self.assertNotIn("abu_decomp.supervisor.gdb", self.argv[0])
        self.assertNotIn("--run-dir", self.argv[0])

    def test_a_plain_play_sets_no_inspection_variables(self):
        self.movie()
        seen = []
        with self.port("1 aa\n", seen):
            tas.play(self.profile, "m", now=T0)
        self.assertFalse([k for k in seen[0] if k.startswith(("N64C_TAS_SHOT", "N64C_TAS_DUMP", "N64C_TAS_TRACE_D"))])

    def test_every_play_is_visible_hashed_and_turbo_by_default(self):
        self.movie()
        seen = []
        with self.port("1 aa\n2 bb\n", seen):
            result = tas.play(self.profile, "m", now=T0)
        env, during = seen
        run = Path(result["run"])
        self.assertEqual(env["N64C_VIEW_FPS"], "30")
        self.assertEqual((env["N64C_VIEW"], env["N64C_TAS_HASH"]), (str(run / "view.bmp"), str(run / "hash.txt")))
        self.assertEqual(env["N64C_SPEED"], "0")
        self.assertEqual(env["N64C_AUDIO_OUT"], str(run / "audio.pcm"))  # every speed (the port paces turbo sound)
        self.assertEqual(env["N64C_INPUT"], tas.input_address(run))  # any run can be taken over
        self.assertRegex(env["N64C_INPUT"], r"^@abu-tas-[0-9a-f]{24}$")  # abstract: no socket path length limit
        self.assertEqual((during["status"], during["movie"], during["speed"], during["polls"]), ("running", "m", 0, 2))
        record = tas.read_record(run)
        self.assertEqual((record["status"], record["outcome"], record["polls_reached"]), ("done", "ok", 2))
        self.assertIsNone(result["same_twice"])  # nothing to compare with yet
        self.assertEqual(tas.last_poll(run / "hash.txt"), 2)

    def test_a_baked_site_gives_the_port_its_reach_rows(self):
        self.movie()
        site = self.root / "runs" / "site"
        site.mkdir(parents=True)
        (site / "manifest.json").write_text(json.dumps({"fnrows": "fnrows.abc.txt"}))
        (site / "fnrows.abc.txt").write_text("main\nfoo\n")
        seen = []
        with self.port("1 aa\n", seen):
            result = tas.play(self.profile, "m", now=T0)
        self.assertEqual(seen[0]["N64C_REACH_ROWS"], str(site / "fnrows.abc.txt"))
        self.assertEqual(tas.reach_address(Path(result["run"])), seen[0]["N64C_INPUT"] + ".reach")

    def test_an_unbaked_site_gives_the_port_no_reach_rows(self):
        self.movie()
        seen = []
        with self.port("1 aa\n", seen):
            tas.play(self.profile, "m", now=T0)
        self.assertNotIn("N64C_REACH_ROWS", seen[0])

    def test_a_lane_runs_its_own_build_under_its_label(self):
        self.movie()
        exe = self.root / "lane-port"
        exe.write_text("lane build")
        exe.chmod(0o755)
        seen = []
        with self.port("1 aa\n2 bb\n", seen):
            result = tas.play(self.profile, "m", now=T0, port=exe, label="TAS-CYCLE-1")
        during = seen[1]
        self.assertEqual((during["port"], during["label"]), (str(exe.resolve()), "TAS-CYCLE-1"))
        self.assertEqual(during["build"], tas.build_id(exe))
        self.assertEqual(tas.read_record(Path(result["run"]))["label"], "TAS-CYCLE-1")
        with self.assertRaises(DecompError):
            tas.play(self.profile, "m", port=self.root / "missing")
        with self.assertRaises(DecompError):
            tas.play(self.profile, "m", label="no spaces")

    def test_the_expansion_pak_is_a_header_a_flag_and_a_record(self):
        self.movie()  # a movie from before the switch: no pak line, so it ran with the pak
        argvs = []

        class Port:
            def __init__(self, argv, env, stdout, **kwargs):
                argvs.append(argv)
                Path(env["N64C_TAS_RECORD"]).write_text("8000 0 0\n")
                Path(env["N64C_TAS_HASH"]).write_text("1 aa\n")
                self.pid = 1
                self.stdin = io.StringIO()

            def wait(self, timeout=None):
                return 0

        with mock.patch.object(tas.subprocess, "Popen", Port):
            old = tas.play(self.profile, "m", now=T0)
            off = tas.play(self.profile, "m", now=T0, pak="off")
            again = tas.play(self.profile, "m", now=T0, pak="off")
            human = tas.play(self.profile, None, now=T0, pak="off")
            with self.assertRaises(DecompError):
                tas.play(self.profile, "m", pak="maybe")
        self.assertNotIn("--pak", argvs[0])
        self.assertEqual(argvs[1][-2:], ["--pak", "off"])
        self.assertEqual([tas.read_record(Path(r["run"]))["pak"] for r in (old, off, human)], ["on", "off", "off"])
        self.assertIsNone(off["same_twice"])  # never compared with the run that had the pak
        self.assertIs(again["same_twice"], True)
        self.assertEqual(again["compared_with"], Path(off["run"]).name)
        self.assertEqual(tas.decode((self.root / "movies" / f"{human['saved']}.tas").read_text())[0]["pak"], "off")
        self.live.mkdir(parents=True, exist_ok=True)
        (self.live / "x.raw").write_text("0000 0 0\n")
        tas.save(self.profile, "short", source=self.live / "x.raw", note="", replace=False, pak="off")
        self.assertEqual(tas.movie_pak(tas.settings(self.profile), "short"), "off")
        self.assertEqual(tas.movie_pak(tas.settings(self.profile), "m"), "on")
        with mock.patch.object(tas.subprocess, "Popen", Port):
            kept = tas.play(self.profile, "short", now=T0)  # the movie's own setting is kept
        self.assertEqual(argvs[-1][-2:], ["--pak", "off"])
        self.assertEqual(tas.read_record(Path(kept["run"]))["pak"], "off")
        self.assertEqual({m["name"]: m["pak"] for m in tas.listing(self.profile)["movies"]}["short"], "off")

    def test_framerate_unlock_is_a_flag_and_a_record_and_nothing_else(self):
        self.movie()
        argvs, envs = [], []

        class Port:
            def __init__(self, argv, env, stdout, **kwargs):
                argvs.append(argv)
                envs.append(env)
                Path(env["N64C_TAS_RECORD"]).write_text("8000 0 0\n")
                Path(env["N64C_TAS_HASH"]).write_text("1 aa\n2 bb\n")  # the same state every poll, unlock or not
                unlocked = "--fps-unlock" in argv
                stdout.write(
                    "boot-fps: running unlock=0 speed=1 retrace=600 wall=10.00 presented=300 (30.0/s) "
                    "game=300 (30.0/s) interpolated=0\n"
                )
                stdout.write(
                    f"boot-fps: ended unlock={int(unlocked)} speed=1 retrace=1200 wall=20.00 "
                    + (
                        "presented=2400 (120.0/s) game=600 (30.0/s) interpolated=1800\n"
                        if unlocked
                        else "presented=600 (30.0/s) game=600 (30.0/s) interpolated=0\n"
                    )
                )
                self.pid = 1
                self.stdin = io.StringIO()

            def wait(self, timeout=None):
                return 0

        with mock.patch.object(tas.subprocess, "Popen", Port):
            off = tas.play(self.profile, "m", now=T0)
            on = tas.play(self.profile, "m", now=T0, fps_unlock=True)
            human = tas.play(self.profile, None, now=T0, fps_unlock=True)
        self.assertNotIn("--fps-unlock", argvs[0])
        self.assertEqual(argvs[1][-1], "--fps-unlock")
        plain = lambda argv: [a for a in argv if "/plays/" not in a]  # noqa: E731 - each run has its own folder
        self.assertEqual(
            plain(argvs[1])[:-1], plain(argvs[0])
        )  # the one option; the command line is otherwise the same
        self.assertEqual(argvs[2][-1], "--fps-unlock")
        for key in ("N64C_SPEED", "N64C_TAS_PLAY", "N64C_TAS_EXIT_AFTER"):
            self.assertEqual(envs[0].get(key) is None, envs[1].get(key) is None)
        self.assertIs(on["same_twice"], True)  # a run with the box on is compared with the one without: the same state
        self.assertEqual(on["compared_with"], Path(off["run"]).name)
        records = [tas.read_record(Path(r["run"])) for r in (off, on, human)]
        self.assertEqual([r["fps_unlock"] for r in records], [False, True, True])
        self.assertEqual(records[0]["shown"]["presented_per_s"], 30.0)
        self.assertEqual((records[1]["shown"]["presented_per_s"], records[1]["shown"]["game_per_s"]), (120.0, 30.0))
        self.assertEqual((records[1]["shown"]["unlock"], records[1]["shown"]["interpolated"]), (True, 1800))
        self.assertIsNone(tas.fps_shown("no such line\n"))
        self.assertNotIn("fps_unlock", tas.decode((self.root / "movies" / f"{human['saved']}.tas").read_text())[0])

    def test_a_profile_cpak_argument_never_replaces_the_runs_card(self):
        cfg = {**tas.settings(self.profile), "args": ["--cpak", "off", "--user-dir", "{user_dir}", "--cpak", "x"]}
        card = Path("/runs/r/card.mpk")
        self.assertEqual(tas._argv(cfg, Path("/u"), card=card)[1:], ["--cpak", str(card), "--user-dir", "/u"])
        self.assertEqual(tas._argv(cfg, Path("/u"))[1:], ["--cpak", "off", "--user-dir", "/u"])

    def test_the_controller_pak_is_a_header_a_card_copy_and_a_record(self):
        self.movie()  # a movie from before the card: no cpak line, so its slot was empty
        runs, ends = [], []

        class Port:
            def __init__(self, argv, env, stdout, **kwargs):
                runs.append(argv)
                if "--cpak" in argv and argv[argv.index("--cpak") + 1] != "off":
                    card = Path(argv[argv.index("--cpak") + 1])
                    if not card.exists():
                        card.write_bytes(tas.fresh_card())  # the port makes a missing card
                    ends.append(card.read_bytes())
                    image = bytearray(card.read_bytes())
                    image[0x300] ^= 0xFF  # the game saved something
                    card.write_bytes(bytes(image))
                Path(env["N64C_TAS_RECORD"]).write_text("8000 0 0\n")
                Path(env["N64C_TAS_HASH"]).write_text("1 aa\n")
                self.pid = 1
                self.stdin = io.StringIO()

            def wait(self, timeout=None):
                return 0

        cfg = tas.settings(self.profile)
        profile_card = cfg["runs"] / tas.PROFILE_CARD
        with mock.patch.object(tas.subprocess, "Popen", Port):
            old = tas.play(self.profile, "m", now=T0)
            fresh = tas.play(self.profile, "m", now=T0, cpak="on")
            fresh2 = tas.play(self.profile, "m", now=T0, cpak="on")
            human = tas.play(self.profile, None, now=T0)  # the owner's run: the profile's own card, made on first use
            with self.assertRaises(DecompError):
                tas.play(self.profile, "m", cpak="maybe")
        flag = lambda argv: argv[argv.index("--cpak") + 1]  # noqa: E731
        self.assertEqual(flag(runs[0]), "off")  # an old movie ran with an empty slot, and the port's default is a card
        self.assertEqual([tas.read_record(Path(r["run"]))["cpak"] for r in (old, fresh)], ["none", "fresh"])
        self.assertEqual(ends[0], tas.fresh_card())
        self.assertEqual(ends[1], tas.fresh_card())  # the second replay did not inherit the first one's save
        self.assertEqual(Path(flag(runs[1])).parent, Path(fresh["run"]))
        self.assertNotEqual(Path(flag(runs[1])).read_bytes(), tas.fresh_card())  # that run's own card holds its save
        self.assertIsNone(old["same_twice"])
        self.assertIs(fresh2["same_twice"], True)  # compared only with the run that had the same card
        self.assertEqual(flag(runs[3]), str(profile_card))
        self.assertEqual(tas.decode((self.root / "movies" / f"{human['saved']}.tas").read_text())[0]["cpak"], "fresh")
        # the owner's second run starts from the card the first one saved: the movie keeps those bytes by hash
        with mock.patch.object(tas.subprocess, "Popen", Port):
            second = tas.play(self.profile, None, now=T0 + timedelta(seconds=5))
        sha = tas.decode((self.root / "movies" / f"{second['saved']}.tas").read_text())[0]["cpak"]
        self.assertRegex(sha, r"^[0-9a-f]{64}$")
        kept = self.root / "movies" / f"{sha}.mpk"
        self.assertEqual(hashlib.sha256(kept.read_bytes()).hexdigest(), sha)
        self.assertEqual(tas.movie_cpak(cfg, second["saved"]), sha)
        before = profile_card.read_bytes()
        with mock.patch.object(tas.subprocess, "Popen", Port):
            replay = tas.play(self.profile, second["saved"], now=T0)
        self.assertEqual(ends[-1], kept.read_bytes())  # the replay starts from that card, in a copy
        self.assertNotEqual(Path(flag(runs[-1])), profile_card)
        self.assertEqual(profile_card.read_bytes(), before)  # the user's card is untouched by a replay
        self.assertEqual(tas.read_record(Path(replay["run"]))["cpak"], sha)
        profile_card.write_bytes(b"short")
        with self.assertRaises(DecompError):
            tas.play(self.profile, None, now=T0)
        with mock.patch.object(tas.subprocess, "Popen", Port):
            off = tas.play(self.profile, None, now=T0, cpak="off")
        self.assertEqual((flag(runs[-1]), tas.read_record(Path(off["run"]))["cpak"]), ("off", "none"))
        self.assertEqual({m["name"]: m["cpak"] for m in tas.listing(self.profile)["movies"]}["m"], "none")

    def test_a_fresh_card_is_the_one_the_port_makes(self):
        # tests/test_cpak.c in the shim holds the same constant for n64c_cpak_format
        image = tas.fresh_card()
        self.assertEqual(len(image), 0x8000)
        fnv = 1469598103934665603
        for byte in image:
            fnv = ((fnv ^ byte) * 1099511628211) & (2**64 - 1)
        self.assertEqual(fnv, 0x5731D45387193563)

    def test_a_human_run_and_a_taken_run_are_saved_as_movies(self):
        self.movie()
        seen = []
        with self.port("1 aa\n2 bb\n", seen):
            human = tas.play(self.profile, None, now=T0)
        env, during = seen
        self.assertNotIn("N64C_TAS_PLAY", env)
        self.assertEqual((env["N64C_SPEED"], env["N64C_VIEW_FPS"]), ("1", "30"))
        self.assertEqual(
            (during["input"], during["label"], during["movie"]), ("human", "owner", "play-20261020-190000")
        )
        self.assertEqual(human["saved"], "play-20261020-190000")
        _, polls = tas.decode((self.root / "movies" / "play-20261020-190000.tas").read_text())
        self.assertEqual(polls, [(0x8000, 0, 0)] * 2)
        self.assertIsNone(human["same_twice"])

        def take(argv, env, stdout, **kwargs):  # the page takes the replay over while it runs
            Path(argv[argv.index("--run-dir") + 1], "taken").write_text("page\n")

        with self.port("1 aa\n2 bb\n", []):
            first = tas.play(self.profile, "m", now=T0)
        port = self.port("1 aa\n2 cc\n", [])
        with port as Port:
            init = Port.__init__

            def both(self_, argv, env, stdout, **kwargs):
                init(self_, argv, env, stdout, **kwargs)
                take(argv, env, stdout)

            Port.__init__ = both
            taken = tas.play(self.profile, "m", now=T0)
        self.assertTrue(taken["taken"])
        self.assertTrue(taken["saved"].startswith("m-take-"))
        self.assertIsNone(taken["same_twice"])  # never compared with the movie's replays ...
        with self.port("1 aa\n2 bb\n", []):
            again = tas.play(self.profile, "m", now=T0)
        self.assertEqual(again["compared_with"], Path(first["run"]).name)  # ... and never their reference
        self.assertIs(again["same_twice"], True)

    def test_a_retry_replays_the_runs_own_polls_then_hands_over_at_the_poll(self):
        self.movie()
        with self.port("1 aa\n2 bb\n", []):
            first = tas.play(self.profile, "m", now=T0)
        source = Path(first["run"]).name
        with self.assertRaises(DecompError):
            tas.play(self.profile, None, source=source, take_at=3)  # its recording has 2 polls
        with self.assertRaises(DecompError):
            tas.play(self.profile, None, source=source, take_at=1, oracle=True)
        seen = []
        port = self.port("1 aa\n2 cc\n", seen)
        with port as Port:
            init = Port.__init__

            def took(self_, argv, env, stdout, **kwargs):
                init(self_, argv, env, stdout, **kwargs)
                stdout.write("n64c-tas: live input took over at poll 2 (N64C_TAS_TAKE_AT)\n")

            Port.__init__ = took
            retry = tas.play(self.profile, None, source=source, take_at=1)
        env, during = seen
        run = Path(retry["run"])
        self.assertEqual((env["N64C_TAS_TAKE_AT"], env["N64C_SPEED"], env["N64C_VIEW_FPS"]), ("1", "0", "30"))
        self.assertEqual((run / "movie.raw").read_text(), "8000 0 0\n")  # the run's own polls up to the poll
        self.assertEqual((during["movie"], during["input"], during["from"], during["at"]), ("m", "human", source, 1))
        self.assertTrue((run / "taken").is_file())
        self.assertTrue(retry["saved"].startswith("m-take-"))
        header, _ = tas.decode((self.root / "movies" / f"{retry['saved']}.tas").read_text())
        self.assertEqual((header["from"], header["at"]), ("m", "1"))
        self.assertEqual(tas.took_over_at(run), 1)

    def test_take_root_strips_every_take_suffix(self):
        self.assertEqual(
            tas.take_root("weird-20261022-032052-take-20261023-0118-take-20261023-031651"), "weird-20261022-032052"
        )
        self.assertEqual(tas.take_root("boot-idle"), "boot-idle")

    def test_archive_keeps_pinned_running_recent_and_each_movies_newest_run(self):
        plays = self.root / "runs" / "plays"
        old = 1000.0
        for name, movie, ended in (("a", "m", old), ("b", "m", old + 1), ("c", "n", old), ("d", "m", 10**10)):
            (plays / name).mkdir(parents=True)
            tas._write_record(
                plays / name, {"movie": movie, "status": "done", "outcome": "ok", "started": ended, "ended": ended}
            )
        (plays / "p").mkdir()
        tas._write_record(plays / "p", {"movie": "m", "status": "done", "outcome": "ok", "started": old, "ended": old})
        tas.pin(tas.settings(self.profile), "p", True)
        gone = tas.archive(tas.settings(self.profile), 3, now=old + 10 * 86400)
        self.assertEqual(sorted(gone), ["a", "b"])  # c is n's newest, d is recent and m's newest, p is pinned
        self.assertEqual(sorted(x.name for x in plays.iterdir()), ["c", "d", "p"])

    def test_speed_one_plays_sound_and_waits_for_the_movie_in_real_time(self):
        self.movie("A *600\n")
        seen, waited = [], []

        def wait(process, timeout, run=None):
            waited.append(timeout)
            return process.wait(), 1.0

        with self.port("1 aa\n", seen), mock.patch.object(tas, "_wait", wait):
            result = tas.play(self.profile, "m", speed=1, now=T0)
        self.assertEqual(seen[0]["N64C_AUDIO_OUT"], str(Path(result["run"]) / "audio.pcm"))
        self.assertEqual(seen[0]["N64C_SPEED"], "1")
        self.assertAlmostEqual(waited[0], 5 + (600 + 7) / 60)
        with self.assertRaises(DecompError):
            tas.play(self.profile, "m", speed=-1)

    def test_same_twice_compares_with_the_previous_run_on_the_same_build(self):
        self.movie()
        with self.port("1 aa\n2 bb\n", []):
            first = tas.play(self.profile, "m", now=T0)
            second = tas.play(self.profile, "m", now=T0)
        self.assertNotEqual(first["run"], second["run"])
        self.assertTrue(Path(second["run"]).name.endswith("-m-2"))
        self.assertIs(second["same_twice"], True)
        self.assertEqual(second["compared_with"], Path(first["run"]).name)
        with self.port("1 aa\n2 cc\n", []):
            third = tas.play(self.profile, "m", now=T0)
        self.assertIs(third["same_twice"], False)
        (self.root / "port").write_text("rebuilt")  # another build: nothing to compare with
        with self.port("1 aa\n2 cc\n", []):
            fourth = tas.play(self.profile, "m", now=T0)
        self.assertIsNone(fourth["same_twice"])
        with self.port("1 aa\n2 cc\n", []):
            twice = tas.play_twice(self.profile, "m", state_diff=False)
        self.assertIs(twice["same_twice"], True)

    def test_same_twice_ignores_interrupted_hashes(self):
        self.movie()
        with self.port("1 aa\n", []):
            interrupted = tas.play(self.profile, "m", now=T0)
        record = Path(interrupted["run"]) / tas.RECORD
        row = json.loads(record.read_text())
        row["outcome"] = "killed"
        record.write_text(json.dumps(row))
        with self.port("1 aa\n2 bb\n", []):
            first_complete = tas.play(self.profile, "m", now=T0)
            second_complete = tas.play(self.profile, "m", now=T0)
        self.assertIsNone(first_complete["same_twice"])
        self.assertIs(second_complete["same_twice"], True)
        self.assertEqual(second_complete["compared_with"], Path(first_complete["run"]).name)

    def test_a_run_whose_process_vanished_reads_as_killed(self):
        run = self.root / "runs" / "plays" / "20261021T000000Z-m"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"id": run.name, "status": "running", "pid": 2**22 + 12345}))
        ((_, row),) = tas.run_rows(tas.settings(self.profile))
        self.assertEqual((row["status"], row["outcome"]), ("done", "killed"))

    def test_signals_finalize_the_record_kill_the_owned_group_and_restore_handlers(self):
        self.movie()
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        for sig in previous:
            with self.subTest(signal=sig):

                def interrupt(process, timeout, run=None, sig=sig):
                    signal.getsignal(sig)(sig, None)
                    tas._check_play_signal()

                with (
                    self.port("1 aa\n", []),
                    mock.patch.object(tas, "_wait", interrupt),
                    mock.patch.object(tas.os, "killpg") as kill,
                    self.assertRaises(tas.PlayInterrupted) as caught,
                ):
                    tas.play(self.profile, "m", now=T0, state_diff=False)
                self.assertEqual(caught.exception.signum, sig)
                row = tas.run_rows(tas.settings(self.profile))[-1][1]
                self.assertEqual((row["status"], row["outcome"], row["signal"]), ("done", "killed", sig.name))
                self.assertGreaterEqual(row["ended"], row["started"])
                self.assertEqual(row["pgid"], 1)
                kill.assert_called_once_with(1, signal.SIGKILL)
                self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)
                self.assertEqual(tas._play_signal, 0)

    def test_signal_during_reporting_cannot_leave_a_running_record(self):
        self.movie()

        def interrupt(*args):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return None

        with (
            self.port("1 aa\n", []),
            mock.patch.object(tas.tas_coverage, "run_report", interrupt),
            self.assertRaises(tas.PlayInterrupted),
        ):
            tas.play(self.profile, "m", now=T0, state_diff=False)
        row = tas.run_rows(tas.settings(self.profile))[0][1]
        self.assertEqual((row["status"], row["outcome"], row["signal"]), ("done", "killed", "SIGTERM"))

    def test_signal_between_spawn_and_registration_still_kills_the_child(self):
        self.movie()
        process = mock.Mock(pid=42, returncode=None)

        def spawn(*args, **kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return process

        with (
            mock.patch.object(tas.subprocess, "Popen", spawn),
            mock.patch.object(tas.os, "killpg") as kill,
            self.assertRaises(tas.PlayInterrupted),
        ):
            tas.play(self.profile, "m", now=T0, state_diff=False)
        kill.assert_called_once_with(42, signal.SIGKILL)
        row = tas.run_rows(tas.settings(self.profile))[0][1]
        self.assertEqual((row["outcome"], row["pgid"]), ("killed", 42))

    def test_spawn_setup_and_reporting_failures_all_finalize_the_record(self):
        self.movie()
        for target in ("subprocess.Popen", "tas_statediff.begin", "tas_coverage.run_report"):
            with self.subTest(stage=target), self.port("1 aa\n", []):
                with (
                    mock.patch("abu_decomp.tas." + target, side_effect=RuntimeError("failed")),
                    self.assertRaisesRegex(RuntimeError, "failed"),
                ):
                    tas.play(self.profile, "m", now=T0)
                run, row = tas.run_rows(tas.settings(self.profile))[-1]
                self.assertEqual((row["status"], row["outcome"]), ("done", "killed"))
                self.assertEqual(tas.read_record(run)["status"], "done")
                self.assertIn("RuntimeError", row["subject"])

    def test_next_play_persists_stale_records_and_preserves_a_live_lease(self):
        self.movie()
        cfg = tas.settings(self.profile)
        live, stale, legacy = (cfg["runs"] / "plays" / name for name in ("live", "stale", "legacy"))
        for run in (live, stale, legacy):
            run.mkdir(parents=True)
        # Reused PID: a vanished lease still proves the writer is gone.
        tas._write_record(stale, {"status": "running", "pid": os.getpid(), "lease": True})
        tas._write_record(legacy, {"status": "running", "pid": 2**22 + 12345})
        with tas._run_record(live, {"status": "running", "pid": 2**22 + 12345}):
            with self.port("1 aa\n", []):
                tas.play(self.profile, "m", now=T0, state_diff=False)
            self.assertTrue(tas.running(live))
            self.assertEqual(tas.read_record(live)["status"], "running")
            for run in (stale, legacy):
                row = tas.read_record(run)
                self.assertEqual((row["status"], row["outcome"], row["reconciled"]), ("done", "killed", True))
            before = (stale / tas.RECORD).read_bytes()
            tas.reconcile_runs(cfg)
            self.assertEqual((stale / tas.RECORD).read_bytes(), before)

    def test_legacy_live_pid_and_completed_records_are_not_rewritten(self):
        cfg = tas.settings(self.profile)
        for name, row in (
            ("live", {"status": "running", "pid": os.getpid()}),
            ("done", {"status": "done", "outcome": "ok"}),
        ):
            run = cfg["runs"] / "plays" / name
            run.mkdir(parents=True)
            tas._write_record(run, row)
        tas.reconcile_runs(cfg)
        self.assertEqual(tas.read_record(cfg["runs"] / "plays/live")["status"], "running")
        self.assertEqual(tas.read_record(cfg["runs"] / "plays/done")["outcome"], "ok")

    def test_parallel_play_cancellation_finalizes_every_active_run(self):
        self.movie()
        cfg = tas.settings(self.profile)
        (cfg["movies"] / "n.tas").write_bytes((cfg["movies"] / "m.tas").read_bytes())
        barrier = threading.Barrier(2)

        def interrupt(process, timeout, run=None):
            barrier.wait(timeout=5)
            with tas._starting_play_process():
                tas._cancel_play(signal.SIGTERM, None)

        with (
            self.port("1 aa\n", []),
            mock.patch.object(tas, "_wait", interrupt),
            mock.patch.object(tas.os, "killpg") as kill,
            self.assertRaises(tas.PlayInterrupted),
        ):
            tas.play_all(self.profile, jobs=2, now=T0, state_diff=False)
        rows = tas.run_rows(cfg)
        self.assertEqual(len(rows), 2)
        self.assertEqual([(row["status"], row["outcome"]) for _, row in rows], [("done", "killed")] * 2)
        self.assertEqual(kill.call_count, 2)

    def test_aborted_record_finishes_the_oracle_and_reaps_all_owned_children(self):
        run = self.root / "runs/plays/aborted"
        run.mkdir(parents=True)
        processes = [mock.Mock(pid=pid, returncode=None) for pid in (101, 102, 103)]
        reaper = mock.Mock()
        with (
            mock.patch.object(tas.os, "killpg") as kill,
            self.assertRaises(subprocess.TimeoutExpired),
            tas._run_record(run, {"status": "running", "oracle": {"status": "running", "pid": 102}}) as (
                children,
                reapers,
            ),
        ):
            children.extend(processes)
            reapers.append(reaper)
            raise subprocess.TimeoutExpired("port", 5)
        self.assertEqual(kill.call_args_list, [mock.call(pid, signal.SIGKILL) for pid in (101, 102, 103)])
        reaper.join.assert_called_once_with(timeout=5)
        for process in processes:
            process.wait.assert_called_once_with(timeout=5)
        row = tas.read_record(run)
        self.assertEqual(
            (row["outcome"], row["oracle"]["status"], row["oracle"]["outcome"]), ("timeout", "done", "timeout")
        )

    def test_fn_of_names_the_function_an_outcome_points_at(self):
        self.assertEqual(tas.fn_of({"subject": "func_80256C90_de+0x10"}), "func_80256C90_de")
        self.assertEqual(tas.fn_of({"subject": "func_8041CAD8_de"}), "func_8041CAD8_de")
        self.assertEqual(
            tas.fn_of({"subject": "host-fault-1", "owner": "func_80209CD8_de at src/a.CPP:3"}), "func_80209CD8_de"
        )
        self.assertIsNone(tas.fn_of({"subject": "exit 3"}))

    def test_table_has_a_same_twice_column(self):
        row = {"movie": "m", "outcome": "ok", "subject": "", "polls_reached": 2, "polls": 2, "vs_recorded": "same"}
        text = tas.table({"movies": [{**row, "same_twice": True, "next": ""}]})
        self.assertIn("same_twice", text)
        self.assertIn(" yes ", text)


class StallTests(unittest.TestCase):
    def test_a_taken_run_is_a_hang_only_after_its_polls_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "taken").write_text("page\n")
            clock = [0.0]

            def monotonic():
                clock[0] += 1.0
                return clock[0]

            def wait4(pid, flags):
                if flags == 0:  # the kill's reap
                    return pid, 9, mock.Mock(ru_utime=1.0, ru_stime=0.0)
                return 0, 0, None

            process = mock.Mock(pid=4242)
            with (
                mock.patch.object(tas.os, "wait4", wait4),
                mock.patch.object(tas.time, "sleep"),
                mock.patch.object(tas.time, "monotonic", monotonic),
                mock.patch.object(tas.os, "killpg") as kill,
                mock.patch.object(tas, "last_poll", lambda path: int(min(clock[0], 100))),  # polls stop at t=100
                mock.patch.object(tas.os, "waitstatus_to_exitcode", return_value=-9),
            ):
                code, _ = tas._wait(process, timeout=5.0, run=run)
            self.assertIsNone(code)
            kill.assert_called_once()
            self.assertGreater(clock[0], 100 + tas.STALL_SECONDS - 2)  # not at the 5 s timeout: after the stall


if __name__ == "__main__":
    unittest.main()
