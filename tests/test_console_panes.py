import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from abu_decomp.console.dashboard_sources import Sources
from abu_decomp.console.feed import append_event
from abu_decomp.console.panes import NextHead, progress_versions

FIXTURES = Path(__file__).parent / "fixtures" / "panes"
# progress.json and the report are copies of the real files one `unbake report` wrote at the same moment.
REPORT = json.loads((FIXTURES / "report-versions.json").read_text())


class FeedProfile:
    def __init__(self, root: Path):
        self.root = root

    def section(self, name):
        assert name == "events"
        return {"path": "events.jsonl"}

    def resolve_path(self, value):
        return self.root / value


class ProgressTests(unittest.TestCase):
    def test_pane_numbers_equal_unbake_report_for_the_same_tree(self):
        versions = progress_versions(json.loads((FIXTURES / "progress.json").read_text()))
        self.assertEqual(sorted(versions), sorted(REPORT["versions"]))
        for version, row in versions.items():
            report = REPORT["versions"][version]
            self.assertEqual(row["code_matched"], report["code_matched"], version)
            self.assertEqual(row["code_total"], report["code_total"], version)
            self.assertEqual(row["data_matched"], report["data_matched"], version)
            self.assertEqual(row["data_total"], report["data_total"], version)
            self.assertEqual(row["functions_matched"], report["functions_matched"], version)
            self.assertEqual(row["functions_total"], report["functions_total"], version)
            self.assertEqual(row["blobs_decoded"], report["blobs_decoded"], version)
            self.assertEqual(row["blobs_total"], report["blobs_total"], version)

    def test_other_schemas_are_refused(self):
        with self.assertRaisesRegex(ValueError, "schema 1"):
            progress_versions({"schema": 1, "carts": []})

    def sources(self, root, panes):
        append_event(FeedProfile(root), "git", "info", "rage-ports abc1234 Fix a thing", kind="LAND")
        return Sources(root / "events.jsonl", title="DECOMP", config={"panes": panes})

    def test_rows_show_each_version_with_the_files_own_numbers_and_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            progress = root / "progress.json"
            progress.write_bytes((FIXTURES / "progress.json").read_bytes())
            os.utime(progress, (1_700_000_000, 1_700_000_000))
            model = self.sources(root, {"progress": {"files": [{"label": "rw", "path": str(progress)}]}}).read()
            by_key = {row.key: row for row in model.rows}
            us = by_key["progress-rw-us"]
            report = REPORT["versions"]["us"]
            self.assertIn(f"{report['code_matched']}/{report['code_total']} bytes matched", us.details)
            self.assertIn(f"fn {report['functions_matched']}/{report['functions_total']}", us.title)
            self.assertIn("22:13:20 UTC", us.details)
            self.assertEqual(len([key for key in by_key if key.startswith("progress-rw-")]), len(REPORT["versions"]))

    def test_next_head_runs_the_command_once_and_caches_a_small_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            progress = root / "progress.json"
            progress.write_bytes((FIXTURES / "progress.json").read_bytes())
            argv = [sys.executable, "-c", f"print(open({str(FIXTURES / 'report-next-head.json')!r}).read())"]
            head = NextHead(argv, root / "cache" / "next.json", count=3)
            head.refresh_if_older_than(progress)
            assert head.thread is not None
            head.thread.join(20)
            self.assertEqual(head.error, "")
            cached = json.loads((root / "cache" / "next.json").read_text())
            self.assertEqual((cached["total"], len(cached["head"])), (8, 3))
            self.assertEqual(cached["head"][0]["name"], "func_80208000_de")
            head.thread = None
            head.refresh_if_older_than(progress)
            self.assertIsNone(head.thread)
            os.utime(progress, (time.time() + 5, time.time() + 5))
            head.refresh_if_older_than(progress)
            self.assertIsNotNone(head.thread)
            head.thread.join(20)

    def test_failed_next_command_is_a_visible_pending_row_not_an_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            progress = root / "progress.json"
            progress.write_bytes((FIXTURES / "progress.json").read_bytes())
            panes = {
                "progress": {
                    "files": [{"label": "rw", "path": str(progress)}],
                    "next": {"argv": [sys.executable, "-c", "raise SystemExit(3)"], "cache": str(root / "n.json")},
                }
            }
            sources = self.sources(root, panes)
            sources.read()
            sources.panes["progress"].next.thread.join(20)
            model = sources.read()
            pending = next(row for row in model.rows if row.key == "next-rw")
            self.assertEqual(pending.state, "planned")
            self.assertIn("-c", pending.details)

    def test_next_rows_come_from_the_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            progress = root / "progress.json"
            progress.write_bytes((FIXTURES / "progress.json").read_bytes())
            cache = root / "n.json"
            document = json.loads((FIXTURES / "report-next-head.json").read_text())["result"]
            cache.write_text(json.dumps({"digest": document["digest"], "total": 2351, "head": document["next"][:2]}))
            os.utime(cache, (time.time() + 60, time.time() + 60))
            panes = {
                "progress": {
                    "files": [{"label": "rw", "path": str(progress)}],
                    "next": {"argv": ["false"], "cache": str(cache)},
                }
            }
            model = self.sources(root, panes).read()
            titles = [row.title for row in model.rows if row.key.startswith("next-rw")]
            self.assertEqual(titles[0], "rw next: 2351 items (unbake report --next)")
            self.assertIn("func_80208000_de (us) 4 B", titles[1])


class LandingsAndLanesTests(unittest.TestCase):
    def test_landings_show_land_events_oldest_first_with_their_age(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = FeedProfile(root)
            append_event(profile, "git", "info", "a noise line")
            append_event(profile, "git", "info", "rage-ports abc1234 first", kind="LAND")
            append_event(profile, "git", "info", "rage-ports def5678 second", kind="LAND")
            model = Sources(root / "events.jsonl", title="DECOMP", config={"panes": {"landings": {}}}).read()
            self.assertEqual(
                [row.title for row in model.rows], ["rage-ports abc1234 first", "rage-ports def5678 second"]
            )
            self.assertTrue(all(row.when for row in model.rows))
            self.assertIn("2 landings", model.heading)

    def test_lanes_show_live_rows_pulse_blocked_state_and_transcript_age(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "events.jsonl").write_text("")
            transcript = root / "t.jsonl"
            transcript.write_text("{}\n")
            old = time.time() - 600
            os.utime(transcript, (old, old))
            (root / "BT-SOL-3").mkdir()
            (root / "BT-SOL-3" / "PULSE.json").write_bytes((FIXTURES / "pulse-real.json").read_bytes())
            rows = [
                {
                    "name": "BT-SOL-3",
                    "kind": "impl",
                    "model": "sol",
                    "track": "official",
                    "state": "live",
                    "transcript": str(transcript),
                    "unit": "lane-BT-SOL-3",
                    "owns": ["/w/bt"],
                },
                {"name": "OLD", "state": "ended"},
            ]
            (root / "lanes.json").write_text(json.dumps({"schema": 1, "lanes": rows}))
            panes = {"lanes": {"registry": str(root / "lanes.json"), "pulse": str(root / "{name}" / "PULSE.json")}}
            model = Sources(root / "events.jsonl", title="DECOMP", config={"panes": panes}).read()
            lane = next(row for row in model.rows if row.key == "lane-BT-SOL-3")
            self.assertIn("impl/sol/official", lane.title)
            self.assertIn("auto: working item/started", lane.title)
            self.assertIn("last write 10m ago", lane.details)
            self.assertIn("owns: /w/bt", lane.details)
            self.assertEqual([row.key for row in model.rows], ["lane-BT-SOL-3"])
