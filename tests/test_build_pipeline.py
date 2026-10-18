import contextlib
import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from abu_decomp.build.runner import Runner
from abu_decomp.build.snapshot import verify_snapshot
from abu_decomp.build.util import BuildFailure
from abu_decomp.cli import main


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


def commit(repo, message):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class Profile:
    def __init__(self, path, data):
        self.source_file = path
        self.project_root = path.parent / "project"
        self.runtime_root = path.parent / "runtime"
        self.output_root = path.parent / "output"
        self.data = data
        self.save()

    def save(self):
        self.source_file.write_text(json.dumps(self.data))
        self.digest = hashlib.sha256(self.source_file.read_bytes()).hexdigest()

    def section(self, name):
        return copy.deepcopy(self.data.get(name, {}))

    def resolve_path(self, value):
        path = Path(value)
        return path if path.is_absolute() else self.source_file.parent / path


class BuildPipelineTest(unittest.TestCase):
    def test_path_bearing_identifiers_fail_before_workspace_creation(self):
        for section in ("sources", "inputs", "checks", "commands", "outputs", "releases"):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                build = {"sources": [{"name": "engine"}], "commands": [{"name": "compile"}]}
                build[section] = [{"name": "../../outside"}]
                profile = Profile(root / "profile.json", {"build": build})
                with self.assertRaisesRegex(BuildFailure, f"Unsafe build.{section} identifier"):
                    Runner(profile)
                self.assertFalse((root / "runtime").exists())
                self.assertFalse((root / "outside").exists())

    def test_duplicate_identifiers_fail_before_workspace_creation(self):
        for section in ("sources", "inputs", "checks", "commands", "outputs", "releases"):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                build = {"sources": [{"name": "engine"}], "commands": [{"name": "compile"}]}
                build[section] = [{"name": "same"}, {"name": "same"}]
                profile = Profile(root / "profile.json", {"build": build})
                with self.assertRaisesRegex(BuildFailure, f"Duplicate build.{section} identifier"):
                    Runner(profile)
                self.assertFalse((root / "runtime").exists())

    def make_profile(self, directory):
        root = Path(directory)
        repositories = {}
        commits = {}
        for name in ("engine", "adapter"):
            repo = root / name
            repo.mkdir()
            git(repo, "init", "-b", "main")
            (repo / "source.c").write_text(f"committed {name}\n")
            commits[name] = commit(repo, f"initial {name}")
            repositories[name] = repo
        arena = root / "arena.pack"
        ui = root / "data.pack"
        arena.write_bytes(b"arena bytes")
        ui.write_bytes(b"frontend bytes")
        build_script = (
            "from pathlib import Path; p=Path('${workspace}')/'compiled'; "
            "p.mkdir(parents=True,exist_ok=True); "
            "(p/'port').write_bytes(b'port binary'); "
            "(p/'probe').write_bytes(b'probe binary')"
        )
        data = {
            "build": {
                "workspace": str(root / "runtime" / "build"),
                "sources": [
                    {"name": name, "repo": str(repo), "ref": "refs/heads/main", "protected_top": ["build"]}
                    for name, repo in repositories.items()
                ],
                "inputs": [
                    {
                        "name": "arena",
                        "path": str(arena),
                        "sha256": hashlib.sha256(arena.read_bytes()).hexdigest(),
                        "filename": "arena.pack",
                    },
                    {
                        "name": "ui",
                        "path": str(ui),
                        "sha256": hashlib.sha256(ui.read_bytes()).hexdigest(),
                        "filename": "data.pack",
                    },
                ],
                "commands": [
                    {"name": "compile", "argv": [sys.executable, "-c", build_script]},
                    {
                        "name": "public-fatal",
                        "argv": [sys.executable, "-c", "print('expected owner'); raise SystemExit(1)"],
                        "expected_exit_codes": [1],
                        "expect_all": ["expected owner"],
                        "forbid": ["boot-complete"],
                    },
                ],
                "outputs": [
                    {"name": "port", "path": "${workspace}/compiled/port"},
                    {"name": "probe", "path": "${workspace}/compiled/probe"},
                ],
                "releases": [
                    {
                        "name": "port",
                        "root": "${runtime_root}/releases/port",
                        "revision": "${commit.engine.short7}-${commit.adapter.short7}",
                        "files": [
                            {"source": "output.port", "target": "app"},
                            {"source": "input.ui", "target": "data/data.pack"},
                        ],
                        "activate": [{"argv": [sys.executable, "-c", "print('port ready')"]}],
                    },
                    {
                        "name": "experiment",
                        "root": "${runtime_root}/releases/experiment",
                        "revision": "${commit.engine.short7}-${commit.adapter.short7}",
                        "files": [
                            {"source": "output.probe", "target": "scene-probe"},
                            {"source": "input.arena", "target": "data/arena.pack"},
                        ],
                        "activate": [{"argv": [sys.executable, "-c", "print('probe ready')"]}],
                    },
                ],
            }
        }
        profile = Profile(root / "profile.json", data)
        return profile, repositories, commits

    def test_two_committed_sources_promote_one_verified_release_mechanism(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, repos, commits = self.make_profile(directory)
            (repos["engine"] / "source.c").write_text("WRITER WIP\n")
            (repos["adapter"] / "untracked.txt").write_text("ADAPTER WIP\n")
            runner = Runner(profile)
            first = runner.attempt()
            self.assertEqual(first["status"], "promoted")
            source_root = runner.workspace / "sources"
            self.assertEqual((source_root / "engine/source.c").read_text(), "committed engine\n")
            self.assertFalse((source_root / "adapter/untracked.txt").exists())
            mtime = (source_root / "engine/source.c").stat().st_mtime_ns
            revision = commits["engine"][:7] + "-" + commits["adapter"][:7]
            for role, binary, asset in (
                ("port", "app", "data.pack"),
                ("experiment", "scene-probe", "arena.pack"),
            ):
                current = profile.runtime_root / "releases" / role / "current"
                self.assertEqual(current.resolve().name, revision)
                self.assertTrue((current / binary).is_file())
                self.assertTrue((current / "data" / asset).is_file())
            second = Runner(profile).attempt()
            self.assertEqual(second["status"], "promoted")
            self.assertEqual((source_root / "engine/source.c").stat().st_mtime_ns, mtime)
            receipt = json.loads(Path(second["receipt"]).read_text())
            self.assertEqual(receipt["sources"]["adapter"]["commit"], commits["adapter"])
            self.assertEqual(receipt["steps"][1]["returncode"], 1)
            for name in ("engine", "adapter"):
                source = receipt["sources"][name]
                manifest = Path(source["manifest"])
                self.assertEqual(hashlib.sha256(manifest.read_bytes()).hexdigest(), source["manifest_sha256"])
                self.assertEqual(json.loads(manifest.read_text())["commit"], commits[name])

    def test_failed_gate_never_switches_current(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, _, _ = self.make_profile(directory)
            profile.data["build"]["commands"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(2)"]
            profile.save()
            result = Runner(profile).attempt()
            self.assertEqual(result["status"], "failed")
            self.assertFalse((profile.runtime_root / "releases/port/current").exists())
            self.assertFalse((profile.runtime_root / "releases/experiment/current").exists())

    def test_superseded_adapter_commit_never_promotes_stale_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, repos, commits = self.make_profile(directory)
            repo = repos["adapter"]
            code = (
                "from pathlib import Path; import subprocess; p=Path(" + repr(str(repo)) + "); "
                "(p/'new.c').write_text('new committed source'); "
                "subprocess.run(['git','-C',str(p),'add','-A'],check=True); "
                "subprocess.run(['git','-C',str(p),'-c','user.name=Test','-c',"
                "'user.email=test@example.invalid','commit','-m','newer'],check=True,stdout=subprocess.DEVNULL)"
            )
            profile.data["build"]["commands"].append({"name": "supersede", "argv": [sys.executable, "-c", code]})
            profile.save()
            result = Runner(profile).attempt()
            self.assertEqual(result["status"], "failed")
            self.assertNotEqual(git(repo, "rev-parse", "HEAD"), commits["adapter"])
            self.assertFalse((profile.runtime_root / "releases/port/current").exists())

    def test_activation_failure_restores_both_currents(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, _, _ = self.make_profile(directory)
            profile.data["build"]["releases"][1]["activate"] = [{"argv": [sys.executable, "-c", "raise SystemExit(3)"]}]
            profile.save()
            result = Runner(profile).attempt()
            self.assertEqual(result["status"], "failed")
            self.assertFalse((profile.runtime_root / "releases/port/current").exists())
            self.assertFalse((profile.runtime_root / "releases/experiment/current").exists())
            receipt = json.loads(Path(result["receipt"]).read_text())
            self.assertIn(
                ("activate-experiment-0", 3), [(step["name"], step["returncode"]) for step in receipt["steps"]]
            )

    def test_private_source_mutation_cannot_be_attributed_to_pinned_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, _, _ = self.make_profile(directory)
            code = (
                "from pathlib import Path; p=Path('${source.engine}/source.c'); "
                "p.write_text('edited private source\\n'); "
                "o=Path('${workspace}/compiled'); o.mkdir(exist_ok=True); "
                "(o/'port').write_bytes(p.read_bytes()); (o/'probe').write_bytes(b'probe')"
            )
            profile.data["build"]["commands"][0]["argv"] = [sys.executable, "-c", code]
            profile.save()
            result = Runner(profile).attempt(prove_only=True)
            self.assertEqual(result["status"], "failed")
            receipt = json.loads(Path(result["receipt"]).read_text())
            self.assertIn("Frozen source changed: engine", receipt["error"])
            self.assertFalse((profile.runtime_root / "releases/port/current").exists())

    def test_manifest_rejects_mode_extra_symlink_and_deleted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, _, _ = self.make_profile(directory)
            result = Runner(profile).attempt(prove_only=True)
            self.assertEqual(result["status"], "proved")
            receipt = json.loads(Path(result["receipt"]).read_text())
            source = profile.runtime_root / "build/sources/engine"
            manifest = json.loads(Path(receipt["sources"]["engine"]["manifest"]).read_text())
            generated = source / "build/object.o"
            generated.parent.mkdir()
            generated.write_bytes(b"private build cache")
            verify_snapshot(source, manifest, ["build"])
            tracked = source / "source.c"
            original_mode = tracked.stat().st_mode & 0o777
            tracked.chmod(0o755 if original_mode != 0o755 else 0o644)
            with self.assertRaisesRegex(ValueError, "content or mode"):
                verify_snapshot(source, manifest, ["build"])
            tracked.chmod(original_mode)
            extra = source / "unexpected.c"
            extra.write_text("not archived")
            with self.assertRaisesRegex(ValueError, "unexpected"):
                verify_snapshot(source, manifest, ["build"])
            extra.unlink()
            extra.symlink_to(tracked)
            with self.assertRaisesRegex(ValueError, "symlink"):
                verify_snapshot(source, manifest, ["build"])
            extra.unlink()
            tracked.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                verify_snapshot(source, manifest, ["build"])

    def test_loaded_profile_digest_is_bound_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, _, _ = self.make_profile(directory)
            profile.source_file.write_bytes(profile.source_file.read_bytes() + b" ")
            with self.assertRaisesRegex(BuildFailure, "Loaded project profile bytes changed"):
                Runner(profile)

    def test_decomp_build_once_reads_the_seat_build_section(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, _, commits = self.make_profile(directory)
            root = Path(directory)
            build = dict(profile.data["build"])
            build.update(project_root=str(root / "project"), runtime_root=str(root / "runtime"),
                         output_root=str(root / "output"))  # fmt: skip
            seat = root / "seat.json"
            seat.write_text(json.dumps({"schema_version": 1, "name": "seat", "build": build}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--profile", str(seat), "build", "once"])
            envelope = json.loads(output.getvalue())
            self.assertEqual(code, 0, envelope)
            self.assertEqual(envelope["result"]["status"], "promoted")
            current = (root / "runtime/releases/port/current").resolve()
            self.assertEqual(current.name, f"{commits['engine'][:7]}-{commits['adapter'][:7]}")
            with contextlib.redirect_stdout(io.StringIO()) as status:
                main(["--profile", str(seat), "build", "status"])
            self.assertEqual(json.loads(status.getvalue())["result"]["latest"]["status"], "promoted")
            del build["runtime_root"]
            seat.write_text(json.dumps({"schema_version": 1, "name": "seat", "build": build}))
            with contextlib.redirect_stdout(io.StringIO()) as missing:
                self.assertEqual(main(["--profile", str(seat), "build", "once"]), 1)
            self.assertIn("runtime_root", json.loads(missing.getvalue())["findings"][0]["message"])


if __name__ == "__main__":
    unittest.main()
