"""Test repos start from git with no host template: a host init.templatedir carries commit-msg and pre-push hooks
(subject <= 72, remote tip known locally) that refuse the throwaway repos the tests build.

Every test also gets its own empty slot pool and no real gcovr: a test must never queue behind (or hold a slot of) the
live rig pool that the lanes build through."""

import atexit
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

_EMPTY = tempfile.mkdtemp(prefix="git-template-")
os.environ["GIT_TEMPLATE_DIR"] = _EMPTY
atexit.register(shutil.rmtree, _EMPTY, ignore_errors=True)

_REAL_RUN = subprocess.run


def _run_without_gcovr(command, *args, **kwargs):
    """gcovr is a real, minutes-long process: no unit test runs it (a test that checks the call mocks run itself)."""
    if isinstance(command, (list, tuple)) and "gcovr" in command and command[:1] == [sys.executable]:
        return subprocess.CompletedProcess(command, 0, "", "")
    return _REAL_RUN(command, *args, **kwargs)


@pytest.fixture(autouse=True)
def _stand_in_seat(request, monkeypatch):
    """A play without --port needs a shared seat whose build states the project's current main (tas_seat); tests that
    drive play with a stand-in port skip that check, and test_tas_seat.py is the one place that runs it."""
    if request.module.__name__.rpartition(".")[2] != "test_tas_seat":
        from abu_decomp import tas_seat

        monkeypatch.setattr(tas_seat, "check", lambda cfg: None)


@pytest.fixture(autouse=True)
def _isolated_rig(tmp_path, monkeypatch):
    monkeypatch.setenv("ABU_SLOTS_DIR", str(tmp_path / "slots"))
    from abu_decomp import slots

    monkeypatch.setattr(slots, "DISK_FLOOR_GIB", 0.0)  # the real disk's free space decides no unrelated test
    monkeypatch.setattr(subprocess, "run", _run_without_gcovr)
