"""The one console row framework (kit) and what each pane builds with it: decomp rows, the port view, the guard on
the viewer's playable stream role."""

import json
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from textual.markup import to_content

from abu_decomp.console import kit, port
from abu_decomp.console.activity import ActivityPane
from abu_decomp.console.dashboard_model import Row
from abu_decomp.console.dashboard_sources import Sources
from abu_decomp.stream import control

THREADS = """\
Thread 3 (Thread 0xc5afeb40 (LWP 11) "rage_decomp_boo"):
#0  0xf78da31b in ?? () from /lib/i386-linux-gnu/libc.so.6
#3  0xf78d0f51 in pthread_cond_wait () from /lib/i386-linux-gnu/libc.so.6
#4  0x08099620 in osRecvMesg ()
#5  0x08087372 in func_802524B0_de ()
#6  0x08098ea7 in thread_main ()

Thread 2 (Thread 0xc63ffb40 (LWP 10) "rage_decomp_boo"):
#3  0xf78d0f51 in pthread_cond_wait () from /lib/i386-linux-gnu/libc.so.6
#4  0x0809ac32 in n64c_sched_halt ()
#5  0x0808f7ae in live_stop ()
#6  0x0809037a in n64c_boot_missing ()
#7  0x0808ee9b in func_80256E6C_de ()
#8  0x08093853 in alInit ()
#9  0x08083693 in func_80256880_de () at src/audio/init.c:41
#10 0x08098ea7 in thread_main ()

Thread 1 (Thread 0xf6f93940 (LWP 9) "rage_decomp_boo"):
#0  0x08090300 in n64c_boot_stopped ()
#1  0x08090276 in n64c_boot_frame_live ()
#2  0x0807bbb3 in main ()
"""
LOG = """\
2026-10-20T04:45:01.515404+00:00 boot-step: 1/932 func_802A71C0_de
2026-10-20T04:45:01.615404+00:00 boot-step: 59/932 func_80256880_de
2026-10-20T04:45:01.715404+00:00 boot-missing: func_80256E6C_de
2026-10-20T04:45:01.815404+00:00 boot-step: census 60/932 hit, 172 watched, last func_80256880_de (order 73), next x
2026-10-20T04:45:01.915404+00:00 debug-stop: n64c_boot_stopped
2026-10-20T04:45:02.015404+00:00 debug-reason: func_80256E6C_de
"""


class KitTests(unittest.TestCase):
    def test_row_label_is_clock_delta_marker_title_count_tag(self):
        base = time.mktime((2026, 10, 19, 21, 42, 50, 0, 0, -1))
        row = Row("k", "func_8027CCA0_eu [1.9 KB] score  CRACKED!", "", "cracked", when=base + 3, tag="L1", count=2)
        self.assertEqual(
            kit.label(row, base),
            "[dim]21:42:53  +3s[/dim] ★ func_8027CCA0_eu \\[1.9 KB] score  CRACKED! x2  [dim]L1[/dim]",
        )
        self.assertEqual(kit.label(Row("u", "frame", ""), base), "[dim]" + " " * 13 + "[/dim] • frame")
        self.assertEqual(kit.label(Row("m", "a[b]", "", when=base), None), "[dim]21:42:50     [/dim] • a\\[b]")

    def test_row_label_with_unclosed_brackets_parses_and_shows_the_text(self):
        # the 10:05:12 stream crash: a title cut mid-list ended in "[112, 113, 114, 1" before the dim tag
        title = "build  failed exit 2: AssertionError: ('uninterpreted nonzero component bytes', [112, 113, 114, 1"
        row = Row("k", title, "", "stop", when=time.time(), tag="BLOBS-ASTRA-1")
        self.assertIn("! " + title + "  BLOBS-ASTRA-1", to_content(kit.label(row, None)).plain)
        self.assertIn(title, to_content(kit.safe_label(row, None)).plain)

    def test_unparseable_label_falls_back_to_plain_text(self):
        row = Row("k", "x\\", "", "stop", when=time.time())
        with unittest.mock.patch.object(kit, "label", return_value="[dim]a[/dim][/dim]"):
            self.assertIn("x", to_content(kit.safe_label(row, None)).plain)

    def test_sizes_cells_heading_and_blocks(self):
        self.assertEqual([kit.size(n) for n in (None, 812, 1964, 20480)], ["", "[812 B]", "[1.9 KB]", "[20.0 KB]"])
        self.assertEqual(kit.cells("f", "", "score", "88.1% match"), "f score  88.1% match")
        self.assertEqual(kit.cells("f", None, "", ""), "f")
        self.assertEqual(kit.heading("PORT", ["boot 1/2", "", "stop x"]), "PORT · boot 1/2 · stop x")
        self.assertEqual(
            kit.block(("in", "a"), ("none", ""), ("long label", "x\ny")), "in          a\nlong label  x\n            y"
        )
        self.assertEqual([kit.delta(s) for s in (0, 12, 240, 7300)], ["+0s", "+12s", "+4m", "+2h"])

    def test_fold_keeps_the_newest_place_counts_and_merges_tags(self):
        rows = [
            Row("1", "unbake score  failed: score.inactive", "m", "stop", when=100.0, tag="A"),
            Row("2", "f score  50.0% match", "", "event", when=101.0, tag="A"),
            Row("3", "unbake score  failed: score.inactive", "m", "stop", when=102.0, tag="B"),
            Row("4", "unbake score  failed: score.inactive", "m", "stop", when=103.0, tag="A"),
        ]
        folded = kit.fold(rows)
        self.assertEqual([r.key for r in folded], ["2", "4"])
        self.assertEqual((folded[1].count, folded[1].tag), (3, "A, B"))
        self.assertIn("seen       3 times", folded[1].details)
        self.assertEqual(
            kit.previous_times([Row("a", "", "", when=5.0), Row("b", "", ""), Row("c", "", "")]), [None, 5.0, 5.0]
        )


def event(stamp, actor, kind, status, message, **payload):
    return {"time": stamp, "actor": actor, "kind": kind, "status": status, "message": message, "payload": payload}


class DecompRowTests(unittest.TestCase):
    def rows(self, events):
        pane = ActivityPane({"fail_seconds": 1e12})
        return pane.rows(SimpleNamespace(records={"feed": events}), [])

    def test_function_rows_read_name_size_operation_value(self):
        stamp = "2026-10-20T04:2{}:00+00:00"
        attempt = dict(
            op="attempt",
            function="func_8027CCA0_eu",
            version="us-rev1",
            size_bytes=1964,
            attempt=8,
            words={"matched": 490, "target": 491, "built": 491},
            exact=False,
            lane="S2",
        )
        rows = self.rows(
            [
                event(stamp.format(1), "S2", "MATCH", "info", "m", **attempt),
                event(stamp.format(2), "S2", "MATCH", "passed", "m", **{**attempt, "exact": True}),
                event(
                    stamp.format(3),
                    "S2",
                    "MATCH",
                    "info",
                    "m",
                    op="recipe",
                    verb="try",
                    ok=True,
                    item="func_8027CCA0_eu",
                    next="recipe name",
                    lane="S2",
                ),
                event(
                    stamp.format(4),
                    "C1",
                    "PROBE",
                    "passed",
                    "m",
                    op="behave",
                    function="func_8027CCA0_eu",
                    verdict="same",
                    checks=9,
                    differences=0,
                    lane="C1",
                ),
                event(
                    stamp.format(5),
                    "C1",
                    "PROBE",
                    "failed",
                    "m",
                    op="behave",
                    function="func_8023C1F0_de",
                    verdict="differs",
                    checks=2,
                    differences=1,
                    reason="store 0x80103D44 differs",
                    lane="C1",
                ),
            ]
        )
        self.assertEqual(
            [(r.title, r.state) for r in rows],
            [
                ("func_8027CCA0_eu [1.9 KB] score  99.7% match", "event"),
                ("func_8027CCA0_eu [1.9 KB] score  CRACKED!", "cracked"),
                ("func_8027CCA0_eu [1.9 KB] recipe try  ok", "event"),
                ("func_8027CCA0_eu [1.9 KB] behave  same as original 9/9", "passed"),
                ("func_8023C1F0_de behave  differs from original 1/2", "stop"),
            ],
        )
        self.assertIn("compares return values and memory writes", rows[3].details)  # what `behave` means
        self.assertIn("store 0x80103D44 differs", rows[4].details)
        self.assertIn("next       recipe name", rows[2].details)

    def test_repeated_identical_tool_failures_fold_into_one_row_with_a_count(self):
        def fail(minute, lane, path):
            message = f"unbake score: failed, {path}:0 score.inactive:"
            return event(
                f"2026-10-20T04:{minute:02d}:00+00:00",
                lane,
                "RUN-FAIL",
                "failed",
                message,
                op="unbake",
                verb="score",
                ok=False,
                seconds=0.7,
                findings=1,
                lane=lane,
            )

        rows = self.rows([fail(1, "S1", "/l/S1/a.c"), fail(2, "S2", "src/b.CPP"), fail(3, "S1", "/l/S1/k3.c")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0].title, rows[0].count, rows[0].tag), ("unbake score  failed: score.inactive", 3, "S1, S2")
        )
        self.assertIn("seen       3 times", rows[0].details)

    def test_build_failures_name_the_tool_output_line(self):
        rows = self.rows(
            [
                event(
                    "2026-10-20T04:46:11+00:00",
                    "S2",
                    "RUN-FAIL",
                    "failed",
                    "Build of x failed (exit 2)",
                    op="build",
                    args=["-C", "rw4"],
                    exit=2,
                    lane="S2",
                    tail=["make: Entering", "make: *** rw4: No such file or directory.  Stop.", "make: Leaving"],
                )
            ]
        )
        self.assertEqual(rows[0].title, "build  failed exit 2: make: *** rw4: No such file or directory.  Stop.")


class PortViewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "output.log").write_text(LOG)
        (self.root / "failure-stack.txt").write_text("#0  0x08090300 in n64c_boot_stopped ()\n")
        self.status = self.root / "port.json"
        self.status.write_text(
            json.dumps(
                {
                    "phase": "held_until_restart",
                    "failure_reason": "func_80256E6C_de",
                    "stack": str(self.root / "failure-stack.txt"),
                    "log": str(self.root / "output.log"),
                    "updated_at": "2026-10-20T04:45:01+00:00",
                }
            )
        )

    def model(self):
        config = {"supervisor_status": str(self.status), "panes": {"lane": {"actor": "PORT-X"}}}
        return Sources(self.root / "events.jsonl", title="PORT", config=config).read()

    def test_title_has_step_and_stop_and_rows_are_the_stopped_game_threads_frames(self):
        (self.root / "failure-threads.txt").write_text(THREADS)
        model = self.model()
        self.assertEqual(
            kit.heading(model.heading, model.facts), "PORT · boot 60/932 · stop func_80256E6C_de not linked"
        )
        self.assertEqual(model.pinned, ["boot 60/932", "stop func_80256E6C_de not linked"])  # the video hud's values
        self.assertEqual(
            [r.title for r in model.rows],
            [
                "#7 func_80256E6C_de",
                "#8 alInit",
                "#9 func_80256880_de  src/audio/init.c:41",
                "1 other game threads",
            ],
        )
        self.assertEqual(model.rows[0].state, "stop")
        self.assertIn("thread 3 rage_decomp_boo: osRecvMesg ← func_802524B0_de", model.rows[-1].details)
        self.assertFalse(any("n64c_" in r.title or "main" in r.title or "thread_main" in r.title for r in model.rows))

    def test_without_a_game_frame_the_rows_are_the_boot_trail_said_as_such(self):
        model = self.model()
        self.assertEqual(
            [r.title for r in model.rows],
            [
                "func_80256E6C_de  stopped here (not linked)",
                "func_80256880_de  last game function reached (order 73)",
            ],
        )

    def test_stack_hide_comes_from_the_tile(self):
        frames = port.frames(THREADS)
        chosen, others = port.stopped_thread(frames, "", ("n64c_", "main", "?", "pthread", "thread_main", "live"))
        self.assertEqual([f["function"] for f in chosen], ["func_80256E6C_de", "alInit", "func_80256880_de"])
        self.assertEqual(list(others), ["thread 3 rage_decomp_boo"])


class PlayerGuardTests(unittest.TestCase):
    def test_tools_cannot_restart_stop_or_type_into_the_playable_role_without_owner(self):
        up = {"roles": {"oracle": {"player": True}, "decomp": {}}}
        for action in ("restart", "stop", "send"):
            with self.assertRaisesRegex(ValueError, "playable instance"):
                control.guard_player(up, "oracle", action, owner=False)
            control.guard_player(up, "oracle", action, owner=True)
            control.guard_player(up, "decomp", action, owner=False)


if __name__ == "__main__":
    unittest.main()
