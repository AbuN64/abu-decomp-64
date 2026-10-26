"""`data stamp` and `data check` refuse a missing or over-72-char `# MSG:` subject through claim-preflight.sh before any build."""

import argparse
import shutil
from pathlib import Path

import pytest

from abu_decomp.data import command, stampbuild, trial

SCRIPT = Path("/mnt/devstorage/Workspaces/RageWars100/orch/bin/claim-preflight.sh")
BODY = "diff --git a/src/a.c b/src/a.c\n--- a/src/a.c\n+++ b/src/a.c\n@@ -1 +1 @@\n-x = 1;\n+x = 2;\n"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="the RageWars100 orch checkout is not on this host")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    bin_ = tmp_path / "home/rig-state/bin"
    bin_.mkdir(parents=True)
    shutil.copy(SCRIPT, bin_ / "claim-preflight.sh")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    built = []

    def reached(*a, **k):
        built.append(a)
        raise AssertionError("a build step ran before the MSG check")

    monkeypatch.setattr(stampbuild, "build", reached)
    monkeypatch.setattr(trial, "Builder", reached)
    monkeypatch.setattr(command, "_cfg", reached)
    return tmp_path, built


def diff(tmp_path, first):
    d = tmp_path / "d.diff"
    d.write_text((first + "\n" if first else "") + BODY)
    return d


def args(verb, d, tmp_path):
    return argparse.Namespace(verb=verb, diff=d, build=True, tree=tmp_path, config=None)


@pytest.mark.parametrize("verb", ["stamp", "check"])
def test_over_long_subject_is_refused_naming_its_length(rig, verb):
    tmp_path, built = rig
    result, code, findings = command.run(args(verb, diff(tmp_path, "# MSG: " + "x" * 73), tmp_path))
    assert code == 1 and not built
    assert "73 chars" in findings[0]["message"] and findings[0]["key"] == "data.msg"


@pytest.mark.parametrize("verb", ["stamp", "check"])
def test_missing_subject_is_refused(rig, verb):
    tmp_path, built = rig
    result, code, findings = command.run(args(verb, diff(tmp_path, ""), tmp_path))
    assert code == 1 and not built and "# MSG:" in findings[0]["message"]


def test_valid_subject_passes_the_check(rig):
    tmp_path, _ = rig
    assert stampbuild.msg_refusal(diff(tmp_path, "# MSG: " + "x" * 72)) == []
