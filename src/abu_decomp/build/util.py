"""Small, deterministic primitives shared by the build runner."""

import os
import re
import subprocess
from pathlib import Path, PurePosixPath

VARIABLE = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


class BuildFailure(Exception):
    """A named gate or integrity check rejected promotion."""

    def __init__(self, message, step=None, steps=None):
        super().__init__(message)
        self.step = step
        self.steps = steps or []
def validate_identifiers(config):
    """Reject path-bearing names and duplicates before creating a workspace."""
    for section in ("sources", "inputs", "checks", "commands", "outputs", "releases"):
        specs = config.get(section, [])
        if not isinstance(specs, list):
            raise BuildFailure(f"build.{section} must be an array")
        seen = set()
        for spec in specs:
            name = spec.get("name") if isinstance(spec, dict) else None
            if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
                raise BuildFailure(f"Unsafe build.{section} identifier: {name!r}")
            if name in seen:
                raise BuildFailure(f"Duplicate build.{section} identifier: {name}")
            seen.add(name)


def expand(value, variables):
    if isinstance(value, str):

        def replace(match):
            key = match.group(1)
            if key not in variables:
                raise BuildFailure(f"Unknown build variable: {key}")
            return str(variables[key])

        return VARIABLE.sub(replace, value)
    if isinstance(value, list):
        return [expand(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: expand(item, variables) for key, item in value.items()}
    return value


def safe_relative(value):
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in (".", "..") for part in path.parts):
        raise BuildFailure(f"Unsafe relative release path: {value!r}")
    return Path(*path.parts)


def run_gate(spec, variables, default_cwd, log_path, on_start=None):
    """Stream command output to a durable log and check exit/output contracts."""
    name = spec["name"]
    argv = expand(spec["argv"], variables)
    if not isinstance(argv, list) or not argv or any(not isinstance(part, str) for part in argv):
        raise BuildFailure(f"{name} must have a nonempty argv array")
    cwd = Path(expand(spec.get("cwd", str(default_cwd)), variables))
    env_override = expand(spec.get("env", {}), variables)
    if not isinstance(env_override, dict):
        raise BuildFailure(f"{name} env must be an object")
    expected = spec.get("expected_exit_codes", [0])
    if not expected or any(not isinstance(code, int) or code < 0 for code in expected):
        raise BuildFailure(f"{name} has invalid expected exit codes")
    timeout = spec.get("timeout_seconds", 1200)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise BuildFailure(f"{name} has invalid timeout")
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    step = {
        "name": name,
        "argv": argv,
        "cwd": str(cwd),
        "env_override": env_override,
        "expected_exit_codes": expected,
        "log": str(log_path),
    }
    environment = os.environ.copy()
    environment.update({key: str(value) for key, value in env_override.items()})
    with log_path.open("w+", encoding="utf-8", buffering=1) as log:
        if on_start:
            on_start(step)
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            step["returncode"] = result.returncode
        except subprocess.TimeoutExpired as error:
            step.update(returncode=None, timed_out_seconds=timeout)
            raise BuildFailure(f"{name} timed out after {timeout} seconds; see {log_path}", step) from error
        except OSError as error:
            log.write(str(error) + "\n")
            step.update(returncode=None, error=str(error))
            raise BuildFailure(f"{name} could not start; see {log_path}", step) from error
        finally:
            log.flush()
            log.seek(0)
            output = log.read()
    if step["returncode"] not in expected:
        raise BuildFailure(f"{name} exited {step['returncode']}; expected {expected}; see {log_path}", step)
    for pattern in spec.get("expect_all", []):
        if not re.search(pattern, output):
            raise BuildFailure(f"{name} missing marker {pattern!r}; see {log_path}", step)
    for pattern in spec.get("forbid", []):
        if re.search(pattern, output):
            raise BuildFailure(f"{name} contained forbidden marker {pattern!r}; see {log_path}", step)
    return step
