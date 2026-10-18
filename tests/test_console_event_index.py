import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_console_panes import FeedProfile

from abu_decomp.console import dashboard_model
from abu_decomp.console.dashboard_model import Event, EventLog, event_addresses
from abu_decomp.console.dashboard_sources import Sources
from abu_decomp.console.feed import append_event


class EventIndexTests(unittest.TestCase):
    def test_event_parses_addresses_once_at_construction(self):
        event = Event({"message": "owner 0x80265964 and 80265965", "payload": {"a": "0x80265966"}})
        self.assertEqual(event.addresses, {"0x80265964", "0x80265965", "0x80265966"})
        with mock.patch.object(dashboard_model, "addresses", side_effect=AssertionError("rescanned")):
            self.assertEqual(event_addresses(event), event.addresses)

    def test_plain_dicts_still_index(self):
        events = EventLog([{"message": "0x80265964"}, {"message": "none"}, {"message": "80265964 again"}])
        self.assertEqual([e["message"] for e in events.by_owner["0x80265964"]], ["0x80265964", "80265964 again"])

    def test_sources_read_scans_addresses_only_for_new_events_and_keeps_order(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / "tmp") as name:
            root = Path(name)
            profile = FeedProfile(root)
            append_event(profile, "git", "info", "first 0x80265964")
            append_event(profile, "git", "info", "second")
            sources = Sources(root / "events.jsonl", title="T", config={"run_status": "x"})
            sources.read()
            self.assertEqual([e["message"] for e in sources.ordered[0]], ["first 0x80265964", "second"])
            first = sources.ordered
            with mock.patch.object(dashboard_model, "addresses", side_effect=AssertionError("rescanned")):
                sources.read()
            self.assertIs(sources.ordered, first)
            append_event(profile, "git", "info", "third 0x80265964")
            sources.read()
            self.assertEqual([e["message"] for e in sources.ordered[0]][-1], "third 0x80265964")
            self.assertEqual(len(sources.ordered[0].by_owner["0x80265964"]), 2)


if __name__ == "__main__":
    unittest.main()
