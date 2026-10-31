"""Runs the unit tests with a per-test timeout, so a hung test fails fast with every thread's stack instead of
blocking the whole suite. The limit is ABU_TEST_TIMEOUT seconds (default 90); the SIGALRM handler raises inside the
hung test, and faulthandler kills the run at 1.5x the limit if the handler cannot get in (a stuck C call)."""

import atexit
import faulthandler
import os
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
LIMIT = float(os.environ.get("ABU_TEST_TIMEOUT", "90"))


class TestTimeout(Exception):
    pass


def expired(signum, frame):
    faulthandler.dump_traceback(all_threads=True)
    raise TestTimeout(f"test exceeded {LIMIT:g}s (set ABU_TEST_TIMEOUT to change)")


class TimedResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        self.seat = stand_in_seat(test)
        faulthandler.dump_traceback_later(LIMIT * 1.5, exit=True)
        signal.setitimer(signal.ITIMER_REAL, LIMIT)

    def stopTest(self, test):
        self.seat.stop()
        signal.setitimer(signal.ITIMER_REAL, 0)
        faulthandler.cancel_dump_traceback_later()
        super().stopTest(test)


def stand_in_seat(test):
    """What tests/conftest.py's `_stand_in_seat` does for pytest: a play with a stand-in port skips the shared-seat
    provenance check, and test_tas_seat.py is the one module that runs it. Started here, stopped by the caller."""
    from abu_decomp import tas_seat

    real = type(test).__module__ == "test_tas_seat"
    patch = mock.patch.object(tas_seat, "check", tas_seat.check if real else lambda cfg: None)
    patch.start()
    return patch


def source_first():
    """The tests import the checkout's src, never an installed abu_decomp (the rig's venv copy): src leads sys.path here
    and PYTHONPATH for the tests' child pythons, as pytest's `pythonpath = ["src"]` does."""
    sys.path.insert(0, str(SRC))
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")]))


def hermetic_git():
    """Tests build their own repos: a host's `init.templatedir` (the rig's carries commit-msg and pre-push hooks and no
    info/) must not shape them, so every git the tests run starts from an empty template."""
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    os.environ[f"GIT_CONFIG_KEY_{count}"] = "init.templateDir"
    os.environ[f"GIT_CONFIG_VALUE_{count}"] = ""
    os.environ["GIT_CONFIG_COUNT"] = str(count + 1)


def hermetic_slots():
    """Keep all test scratch on the workspace disk, including isolated slot pools. A host's /tmp may be a small
    tmpfs that cannot satisfy the production disk floor; the guard still checks the actual workspace capacity."""
    scratch = tempfile.mkdtemp(prefix=".abu-tests-", dir=os.getcwd())
    atexit.register(shutil.rmtree, scratch, ignore_errors=True)
    tempfile.tempdir = scratch
    os.environ["TMPDIR"] = scratch
    pool = tempfile.mkdtemp(prefix="abu-slots-")
    os.environ["ABU_SLOTS_DIR"] = pool


def main(start="tests", *names):
    """Every test under `start`, or only the named test modules (`test_tas_coverage`) when given."""
    source_first()
    hermetic_git()
    hermetic_slots()
    signal.signal(signal.SIGALRM, expired)
    if names:
        sys.path.insert(0, start)
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    else:
        suite = unittest.defaultTestLoader.discover(start)
    result = unittest.TextTestRunner(
        resultclass=TimedResult, verbosity=int(os.environ.get("ABU_TEST_VERBOSITY", "1"))
    ).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
