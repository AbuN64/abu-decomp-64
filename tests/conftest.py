"""Test repos start from git with no host template: a host init.templatedir carries commit-msg and pre-push hooks
(subject <= 72, remote tip known locally) that refuse the throwaway repos the tests build."""

import atexit
import os
import shutil
import tempfile

_EMPTY = tempfile.mkdtemp(prefix="git-template-")
os.environ["GIT_TEMPLATE_DIR"] = _EMPTY
atexit.register(shutil.rmtree, _EMPTY, ignore_errors=True)
