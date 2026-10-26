import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("changed_tests", Path(__file__).parent.parent / "ci" / "changed_tests.py")
changed_tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changed_tests)


class ImportsTests(unittest.TestCase):
    def test_the_three_import_forms_match_the_module(self):
        for line in (
            "from abu_decomp import tas_coverage as cov",
            "from abu_decomp import tas, tas_coverage",
            "import abu_decomp.tas_coverage",
            "from abu_decomp.tas_coverage import gcovr",
        ):
            self.assertTrue(changed_tests.imports(line + "\n", "abu_decomp.tas_coverage"), line)

    def test_other_modules_do_not_match(self):
        for line in (
            "from abu_decomp import tas_corpus",
            "import abu_decomp.tas",
            "# from abu_decomp import tas_coverage",
        ):
            self.assertFalse(changed_tests.imports(line + "\n", "abu_decomp.tas_coverage"), line)


if __name__ == "__main__":
    unittest.main()
