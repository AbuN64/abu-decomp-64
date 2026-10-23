"""Prints the test modules that cover what changed, one per line: `ci/check --changed` runs only these (seconds, not
the whole suite). Changed = uncommitted, untracked and committed-but-unpushed files (against the upstream, else HEAD).

A changed tests/test_X.py maps to itself; a changed src/abu_decomp/.../M.py maps to tests/test_M.py and every test that
imports the module M. Notes on stderr name what the fast path cannot judge (a changed file no test imports, shim/, ci/,
the build files): run the full `ci/check` for those before pushing."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*args: str) -> list[str]:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return done.stdout.split("\n") if done.returncode == 0 else []


def changed() -> set[str]:
    # The merge base, so commits that only the upstream has (a fetch moved it) are not counted as changes.
    base = (git("merge-base", "HEAD", "@{upstream}") or ["HEAD"])[0]
    found = set(git("diff", "--name-only", base)) | set(git("ls-files", "--others", "--exclude-standard"))
    return {name for name in found if name}


def imports(text: str, module: str) -> bool:
    """Does the test source import `module` (dotted): `import a.b`, `from a import b` or `from a.b import x`?"""
    parts = module.split(".")
    parent, leaf = ".".join(parts[:-1]), parts[-1]
    pattern = (
        rf"^\s*(from\s+{re.escape(module)}\s+import\b|import\s+{re.escape(module)}\b"
        rf"|from\s+{re.escape(parent)}\s+import\s+[^\n#]*\b{re.escape(leaf)}\b)"
    )
    return re.search(pattern, text, re.MULTILINE) is not None


def main() -> int:
    if sys.argv[1:] == ["--files"]:  # the changed Python files that exist, for the lint step of `ci/check --changed`
        paths = sorted(n for n in changed() if n.endswith(".py") and (ROOT / n).is_file())
        print("\n".join(paths))
        return 0
    tests = {path.stem: path.read_text() for path in sorted((ROOT / "tests").glob("test_*.py"))}
    picked: set[str] = set()
    unjudged: list[str] = []
    for name in sorted(changed()):
        path = Path(name)
        if path.parts[:1] == ("tests",) and path.name.startswith("test_") and path.suffix == ".py":
            if (ROOT / name).is_file():
                picked.add(path.stem)
        elif path.parts[:2] == ("src", "abu_decomp") and path.suffix == ".py":
            rel = path.with_suffix("").parts[1:]
            module = ".".join(rel[:-1] if rel[-1] == "__init__" else rel)
            hits = {stem for stem, text in tests.items() if imports(text, module)}
            if f"test_{rel[-1]}" in tests:
                hits.add(f"test_{rel[-1]}")
            if hits:
                picked |= hits
            else:
                unjudged.append(f"{name}: no test imports {module}")
        elif path.parts[:1] in {("tests",), ("docs",)} or path.suffix == ".md":
            continue
        else:
            unjudged.append(f"{name}: not judged by module (run the full ci/check)")
    for note in unjudged:
        print(f"ci/check --changed: {note}", file=sys.stderr)
    print("\n".join(sorted(picked)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
