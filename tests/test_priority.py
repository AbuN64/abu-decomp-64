"""Priority by kind of work: proofs raised as far as the host allows, trials niced, everything else untouched."""

from __future__ import annotations

import unittest
from typing import Any

from abu_decomp import priority

P = ["--profile", "/seat.json"]


def _apply(args: list[str], now: int, lowest: int = 0, fail: bool = False) -> tuple[Any, list[Any], list[str]]:
    calls: list[Any] = []
    warned: list[str] = []

    def setnice(value: int) -> None:
        if fail:
            raise PermissionError(13, "denied")
        calls.append(("nice", value))

    got = priority.apply(
        args,
        getnice=lambda: now,
        setnice=setnice,
        lowest=lambda: lowest,
        ioprio=lambda level: calls.append(("io", level)),
        warn=warned.append,
    )
    return got, calls, warned


class Kind(unittest.TestCase):
    def test_land_make_and_tree_setup_are_proofs(self) -> None:
        for line in (
            [*P, "ship", "--tree", "T"],
            ["make", "check"],
            [*P, "unbake", "--project", "T", "--host", "H", "setup"],
            [*P, "unbake", "--", "--project=T", "land"],
        ):
            self.assertEqual(priority.kind(line), "proof", line)

    def test_score_try_and_recipe_are_trials(self) -> None:
        for line in (
            [*P, "unbake", "--project", "T", "score", "f"],
            ["recipe", "try"],
            [*P, "behave", "--function", "f"],
        ):
            self.assertEqual(priority.kind(line), "trial", line)

    def test_other_commands_keep_their_priority(self) -> None:
        for line in ([*P, "lane", "end"], [*P, "stream", "tile"], ["--version"], []):
            self.assertIsNone(priority.kind(line), line)


class Apply(unittest.TestCase):
    def test_a_land_started_under_nice_15_comes_back_to_the_proof_value(self) -> None:
        self.assertEqual(_apply([*P, "ship"], 15, lowest=-5), (-5, [("nice", -5), ("io", 0)], []))

    def test_without_rlimit_room_a_proof_goes_as_low_as_allowed(self) -> None:
        self.assertEqual(_apply([*P, "ship"], 15, lowest=10), (10, [("nice", 10), ("io", 0)], []))

    def test_a_proof_that_cannot_go_lower_runs_with_one_warning(self) -> None:
        got, calls, warned = _apply([*P, "ship"], 15, lowest=20)
        self.assertEqual((got, calls), (None, [("io", 0)]))
        self.assertEqual(len(warned), 1)

    def test_a_raised_proof_is_never_lowered(self) -> None:
        self.assertEqual(_apply(["make"], -10, lowest=-20), (None, [("io", 0)], []))

    def test_a_trial_is_niced_and_never_raised(self) -> None:
        self.assertEqual(_apply([*P, "unbake", "score", "f"], 0), (5, [("nice", 5)], []))
        self.assertEqual(_apply([*P, "unbake", "score", "f"], 10), (None, [], []))

    def test_other_commands_are_untouched(self) -> None:
        self.assertEqual(_apply([*P, "lane", "end"], 3), (None, [], []))

    def test_a_refused_change_never_fails_the_command(self) -> None:
        got, _, warned = _apply([*P, "ship"], 15, lowest=-5, fail=True)
        self.assertIsNone(got)
        self.assertEqual(len(warned), 1)


if __name__ == "__main__":
    unittest.main()


def test_own_unit_is_the_scope_or_service_leaf_of_the_unified_line():
    from abu_decomp import priority

    assert priority.own_unit("0::/user.slice/user@1000.service/decomp.slice/ssh-9.scope\n") == "ssh-9.scope"
    assert priority.own_unit("1:name=systemd:/x\n0::/system.slice/decomp-ship.service\n") == "decomp-ship.service"
    assert priority.own_unit("0::/user.slice/user-1000.slice\n") is None
    assert priority.own_unit("") is None
