"""Run ordered public gates against pinned committed sources and frozen inputs."""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from abu_decomp.files import atomic_json, sha256_file

from .release import prepare, promote
from .snapshot import sync_snapshot, verify_snapshot
from .util import BuildFailure, expand, run_gate, validate_identifiers


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise BuildFailure(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


class Runner:
    def __init__(self, profile):
        self.profile = profile
        self.config = profile.section("build")
        if not self.config.get("sources") or not self.config.get("commands"):
            raise ValueError("build.sources and build.commands are required")
        validate_identifiers(self.config)
        self.profile_hash = profile.digest
        if sha256_file(Path(profile.source_file)) != self.profile_hash:
            raise BuildFailure("Loaded project profile bytes changed before build setup")
        workspace_value = self.config.get("workspace", str(Path(profile.runtime_root) / "build"))
        self.workspace = profile.resolve_path(workspace_value)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def path(self, value, variables=None):
        return self.profile.resolve_path(expand(value, variables or {}))

    def status(self, record, phase, step=None):
        value = {
            "schema": 1,
            "phase": phase,
            "started_at": record["started_at"],
            "updated_at": now(),
            "sources": record.get("sources", {}),
            "last_result": record["status"] if record["status"] != "running" else None,
            "error": record.get("error"),
            "gate": step["name"] if step else None,
            "log_path": step["log"] if step else None,
            "receipt": str(self.workspace / "receipts" / f"{record['id']}.json")
            if phase in ("proved", "promoted", "failed", "blocked")
            else None,
        }
        atomic_json(self.workspace / "status.json", value)

    def pin_sources(self, record, variables, run_dir):
        pinned = {}
        for spec in self.config["sources"]:
            name = spec["name"]
            if name in pinned:
                raise BuildFailure(f"Duplicate source name: {name}")
            repo = self.path(spec["repo"], variables).resolve(strict=True)
            commit = git(repo, "rev-parse", spec["ref"])
            tree = git(repo, "rev-parse", f"{commit}^{{tree}}")
            source = self.workspace / "sources" / name
            manifest = self.workspace / "manifests" / f"{name}.json"
            protected = spec.get("protected_top", [])
            snapshot = sync_snapshot(repo, commit, source, manifest, protected)
            synced = json.loads(manifest.read_text())
            frozen_manifest = run_dir / "manifests" / f"{name}.json"
            frozen = {
                "schema": 1,
                "source": name,
                "commit": commit,
                "tree": tree,
                "archive_sha256": snapshot["archive_sha256"],
                "files": synced["files"],
            }
            atomic_json(frozen_manifest, frozen)
            frozen_manifest.chmod(0o444)
            manifest_hash = sha256_file(frozen_manifest)
            verify_snapshot(source, frozen, protected)
            pinned[name] = {
                "repo": str(repo),
                "ref": spec["ref"],
                "commit": commit,
                "tree": tree,
                "snapshot": snapshot,
                "source": str(source),
                "manifest": str(frozen_manifest),
                "manifest_sha256": manifest_hash,
                "protected_top": protected,
            }
            variables[f"source.{name}"] = str(source)
            variables[f"source.{name}.manifest"] = str(frozen_manifest)
            variables[f"source.{name}.manifest_sha256"] = manifest_hash
            variables[f"commit.{name}.full"] = commit
            variables[f"commit.{name}.short7"] = commit[:7]
            variables[f"tree.{name}"] = tree
        record["sources"] = pinned
        return pinned

    def freeze_inputs(self, record, variables, run_dir):
        frozen = {}
        for spec in self.config.get("inputs", []):
            name = spec["name"]
            if name in frozen:
                raise BuildFailure(f"Duplicate input name: {name}")
            configured = expand(spec["path"], variables)
            raw_alias = Path(configured)
            if not raw_alias.is_absolute():
                raw_alias = Path(self.profile.source_file).parent / raw_alias
            selected = self.path(spec["path"], variables).resolve(strict=True)
            if not selected.is_file() or selected.is_symlink() or not selected.stat().st_size:
                raise BuildFailure(f"Input is missing, empty, or a symlink: {selected}")
            digest = sha256_file(selected)
            if spec.get("sha256") and spec["sha256"] != digest:
                raise BuildFailure(f"Pinned input hash differs: {name}")
            filename = spec.get("filename", selected.name)
            if Path(filename).name != filename:
                raise BuildFailure(f"Input filename must be one name: {filename}")
            destination = run_dir / "inputs" / name / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(selected, destination, follow_symlinks=False)
            if sha256_file(destination) != digest:
                raise BuildFailure(f"Input changed while frozen: {name}")
            frozen[name] = {
                "source": str(selected),
                "configured_path": spec["path"],
                "source_alias": str(raw_alias),
                "frozen": str(destination),
                "sha256": digest,
                "bytes": destination.stat().st_size,
                "source_mtime_ns": selected.stat().st_mtime_ns,
                "metadata": spec.get("metadata", {}),
            }
            variables[f"input.{name}"] = str(destination)
            variables[f"input.{name}.dir"] = str(destination.parent)
            variables[f"input.{name}.sha256"] = digest
        record["inputs"] = frozen

    def verify_checks(self, phase, variables):
        for spec in self.config.get("checks", []):
            if phase not in spec.get("when", ["pre", "post"]):
                continue
            kind = spec["kind"]
            if kind == "file_sha256":
                path = self.path(spec["path"], variables)
                if sha256_file(path) != spec["sha256"]:
                    raise BuildFailure(f"File hash check failed: {spec['name']}")
            elif kind == "git_archive":
                repo = self.path(spec["repo"], variables)
                commit = git(repo, "rev-parse", "HEAD")
                if commit != spec["commit"]:
                    raise BuildFailure(f"Git dependency commit changed: {spec['name']}")
                result = subprocess.run(
                    ["git", "-C", str(repo), "archive", "--format=tar", commit], capture_output=True, timeout=120
                )
                if result.returncode or hashlib.sha256(result.stdout).hexdigest() != spec["sha256"]:
                    raise BuildFailure(f"Git archive check failed: {spec['name']}")
            elif kind == "file_contains":
                data = self.path(spec["path"], variables).read_text(encoding="utf-8")
                if expand(spec["text"], variables) not in data:
                    raise BuildFailure(f"File marker check failed: {spec['name']}")
            elif kind == "path_absent":
                if self.path(spec["path"], variables).exists():
                    raise BuildFailure(f"Unexpected path exists: {spec['name']}")
            else:
                raise BuildFailure(f"Unknown build check kind: {kind}")

    def verify_current(self, record, variables, switched_roots=()):
        if sha256_file(Path(self.profile.source_file)) != self.profile_hash:
            raise BuildFailure("Project profile changed during gates")
        for name, item in record["sources"].items():
            repo = Path(item["repo"])
            if git(repo, "rev-parse", item["ref"]) != item["commit"]:
                raise BuildFailure(f"Committed source was superseded: {name}")
            if git(repo, "rev-parse", f"{item['commit']}^{{tree}}") != item["tree"]:
                raise BuildFailure(f"Committed tree changed: {name}")
            manifest = Path(item["manifest"])
            if sha256_file(manifest) != item["manifest_sha256"]:
                raise BuildFailure(f"Frozen source manifest changed: {name}")
            frozen = json.loads(manifest.read_text())
            if (frozen.get("schema"), frozen.get("commit"), frozen.get("tree"), frozen.get("archive_sha256")) != (
                1,
                item["commit"],
                item["tree"],
                item["snapshot"]["archive_sha256"],
            ):
                raise BuildFailure(f"Frozen source identity changed: {name}")
            try:
                verify_snapshot(Path(item["source"]), frozen, item["protected_top"])
            except ValueError as error:
                raise BuildFailure(f"Frozen source changed: {name}: {error}") from error
        for name, item in record.get("inputs", {}).items():
            alias = Path(item["source_alias"])
            self_switched = any(alias.is_relative_to(Path(root) / "current") for root in switched_roots)
            current = self.path(item["configured_path"], variables)
            if not self_switched and (current != Path(item["source"]) or sha256_file(current) != item["sha256"]):
                raise BuildFailure(f"Input version changed during gates: {name}")
            if sha256_file(Path(item["frozen"])) != item["sha256"]:
                raise BuildFailure(f"Frozen input changed during gates: {name}")
        self.verify_checks("post", variables)

    def stage_outputs(self, record, variables, run_dir):
        outputs = {}
        for spec in self.config.get("outputs", []):
            name = spec["name"]
            if name in outputs:
                raise BuildFailure(f"Duplicate output name: {name}")
            source = self.path(spec["path"], variables)
            if not source.resolve().is_relative_to(self.workspace.resolve()):
                raise BuildFailure(f"Output must come from the private workspace: {name}")
            if not source.is_file() or source.is_symlink() or not source.stat().st_size:
                raise BuildFailure(f"Output missing or empty: {name}")
            if spec.get("executable") and not os.access(source, os.X_OK):
                raise BuildFailure(f"Output is not executable: {name}")
            digest = sha256_file(source)
            destination = run_dir / "artifacts" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
            if sha256_file(destination) != digest:
                raise BuildFailure(f"Staged output differs from built artifact: {name}")
            outputs[name] = {
                "source": str(source),
                "staged": str(destination),
                "sha256": digest,
                "bytes": destination.stat().st_size,
            }
            variables[f"output.{name}"] = str(destination)
            variables[f"output.{name}.sha256"] = digest
        record["outputs"] = outputs

    def attempt(self, prove_only=False):
        record = {
            "id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8],
            "started_at": now(),
            "status": "running",
            "steps": [],
            "profile_sha256": self.profile_hash,
        }
        run_dir = self.workspace / "runs" / record["id"]
        run_dir.mkdir(parents=True)
        variables = {
            "workspace": str(self.workspace),
            "run_dir": str(run_dir),
            "runtime_root": str(self.profile.runtime_root),
            "output_root": str(self.profile.output_root),
            "project_root": str(self.profile.project_root),
        }
        self.status(record, "running")
        try:
            self.pin_sources(record, variables, run_dir)
            self.freeze_inputs(record, variables, run_dir)
            self.verify_checks("pre", variables)
            for spec in self.config["commands"]:
                cwd = self.path(spec.get("cwd", str(self.workspace)), variables)
                if not cwd.resolve().is_relative_to(self.workspace.resolve()):
                    raise BuildFailure(f"Gate cwd escapes the private workspace: {spec['name']}")
                log = self.workspace / "logs" / f"{record['id']}-{spec['name']}.log"

                def started(step):
                    self.status(record, f"gate:{step['name']}", step)

                try:
                    step = run_gate(spec, variables, self.workspace, log, on_start=started)
                    record["steps"].append(step)
                except BuildFailure as error:
                    if error.step:
                        record["steps"].append(error.step)
                    raise
                self.verify_checks(f"after:{spec['name']}", variables)
            self.verify_current(record, variables)
            self.stage_outputs(record, variables, run_dir)
            self.verify_current(record, variables)
            if prove_only:
                record["status"] = "proved"
            else:
                prepared = prepare(self.config["releases"], variables, self.path)
                self.verify_current(record, variables)

                def on_activation(step):
                    self.status(record, f"gate:{step['name']}", step)

                result = promote(
                    prepared,
                    variables,
                    self.workspace / "logs" / record["id"],
                    on_activation,
                    lambda phase: self.verify_current(
                        record, variables, [item["root"] for item in prepared] if phase == "post" else ()
                    ),
                )
                record["releases"] = result["releases"]
                record["steps"].extend(result["steps"])
                record["status"] = "promoted"
        except (
            BuildFailure,
            OSError,
            ValueError,
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            tarfile.TarError,
        ) as error:
            if isinstance(error, BuildFailure):
                for step in error.steps or ([error.step] if error.step else []):
                    if step not in record["steps"]:
                        record["steps"].append(step)
            record["status"] = "failed" if record["steps"] else "blocked"
            record["error"] = str(error)
        record["finished_at"] = now()
        receipt = self.workspace / "receipts" / f"{record['id']}.json"
        atomic_json(receipt, record)
        atomic_json(self.workspace / "latest.json", {"receipt": str(receipt), **record})
        if record["status"] == "promoted":
            atomic_json(self.workspace / "last-success.json", {"receipt": str(receipt), **record})
        self.status(record, record["status"], record["steps"][-1] if record["steps"] else None)
        return {
            "receipt": str(receipt),
            "status": record["status"],
            "sources": {name: item["commit"] for name, item in record.get("sources", {}).items()},
            "error": record.get("error"),
        }

    def drain(self, prove_only=False):
        while True:
            result = self.attempt(prove_only=prove_only)
            receipt = json.loads(Path(result["receipt"]).read_text())
            changed = False
            for item in receipt.get("sources", {}).values():
                if git(Path(item["repo"]), "rev-parse", item["ref"]) != item["commit"]:
                    changed = True
                    break
            if not changed:
                return result
