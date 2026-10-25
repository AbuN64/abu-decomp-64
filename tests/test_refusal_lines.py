import io
import unittest
from contextlib import redirect_stderr

from abu_decomp import cli


class RefusalLinesTests(unittest.TestCase):
    def test_a_blocking_finding_prints_its_key_and_its_one_next_command_on_stderr(self):
        err = io.StringIO()
        with redirect_stderr(err):
            cli._refusal_lines(
                [
                    {
                        "key": "land.commit_subject",
                        "message": "subject is 80 characters",
                        "blocking": True,
                        "next": "decomp ship",
                    },
                    {"key": "ship.note", "message": "fine", "blocking": False},
                ]
            )
        self.assertEqual(
            err.getvalue().splitlines(),
            ["decomp: refused land.commit_subject: subject is 80 characters", "decomp: next: decomp ship"],
        )

    def test_a_recipe_finding_prints_its_reason_and_action(self):
        err = io.StringIO()
        with redirect_stderr(err):
            cli._refusal_lines(
                [
                    {
                        "key": "recipe.name",
                        "reason": "no member or folder is named x",
                        "blocking": True,
                        "action": "recipe name next",
                    }
                ]
            )
        self.assertEqual(
            err.getvalue().splitlines(),
            ["decomp: refused recipe.name: no member or folder is named x", "decomp: next: recipe name next"],
        )


if __name__ == "__main__":
    unittest.main()
