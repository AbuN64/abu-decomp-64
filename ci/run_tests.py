"""Runs the unit tests with a per-test timeout, so a hung test fails fast with every thread's stack instead of
blocking the whole suite. The limit is ABU_TEST_TIMEOUT seconds (default 90); the SIGALRM handler raises inside the
hung test, and faulthandler kills the run at 1.5x the limit if the handler cannot get in (a stuck C call)."""

import faulthandler
import os
import signal
import sys
import unittest

LIMIT = float(os.environ.get("ABU_TEST_TIMEOUT", "90"))


class TestTimeout(Exception):
    pass


def expired(signum, frame):
    faulthandler.dump_traceback(all_threads=True)
    raise TestTimeout(f"test exceeded {LIMIT:g}s (set ABU_TEST_TIMEOUT to change)")


class TimedResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        faulthandler.dump_traceback_later(LIMIT * 1.5, exit=True)
        signal.setitimer(signal.ITIMER_REAL, LIMIT)

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        faulthandler.cancel_dump_traceback_later()
        super().stopTest(test)


def hermetic_git():
    """Tests build their own repos: a host's `init.templatedir` (the rig's carries commit-msg and pre-push hooks and no
    info/) must not shape them, so every git the tests run starts from an empty template."""
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    os.environ[f"GIT_CONFIG_KEY_{count}"] = "init.templateDir"
    os.environ[f"GIT_CONFIG_VALUE_{count}"] = ""
    os.environ["GIT_CONFIG_COUNT"] = str(count + 1)


def main(start="tests"):
    hermetic_git()
    signal.signal(signal.SIGALRM, expired)
    suite = unittest.defaultTestLoader.discover(start)
    result = unittest.TextTestRunner(
        resultclass=TimedResult, verbosity=int(os.environ.get("ABU_TEST_VERBOSITY", "1"))
    ).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
