"""Launch one Codex lane through `codex app-server` (the ButlerMaster route): own server on a unix socket,
JSON-RPC over websocket, fresh threads at the context cap; every frame kept, final answer written out.
usage: lane.py --codex BIN --model M --effort E --cwd DIR --brief FILE --dir OUTDIR --socket PATH --name NAME --wall SECONDS --fanout enabled|disabled --max-agents N
Every value is required; nothing is defaulted. Exit 0 = turn completed with a final answer; 6 = --wall reached;
7 = the account's usage limit; 8 = rollover failed (preserved state, see stderr/transcript).
Self-test: `lane.py --self-test` runs the stop rule's own checks and exits 0/1; no codex or network needed."""

import argparse, atexit, base64, ctypes, json, os, select, signal, socket, stat, struct, subprocess, sys, tempfile, time


def cleanup_lane(directory, dry_run=False):
    """Remove disposable lane builds using directory FDs; preserve complete Git worktrees.

    The root and its ancestors must be physical directories. Every mutation is relative to an
    open descriptor beneath that root, so a clone's link cannot redirect deletion outside --dir.
    Unknown directories and all top-level files (including results and evidence) remain.
    """
    root = os.path.abspath(directory)
    if root != os.path.realpath(root) or root == os.path.sep:
        print("CLEANUP REFUSED: --dir is root or has a symlink component: " + root, flush=True)
        return False
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    rootfd = None
    try:
        # Open every ancestor relative to an already checked directory, closing the check/open race.
        rootfd = os.open(os.path.sep, flags)
        for component in root.split(os.path.sep)[1:]:
            childfd = os.open(component, flags, dir_fd=rootfd)
            os.close(rootfd)
            rootfd = childfd
    except OSError as error:
        if rootfd is not None:
            os.close(rootfd)
        print("CLEANUP REFUSED: --dir: %s: %s" % (root, error), flush=True)
        return False
    try:
        logfd = os.open("launch.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=rootfd)
        with os.fdopen(logfd, "a", buffering=1) as cleanup_log:

            def note(message):
                cleanup_log.write("CLEANUP %s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), message))

            note("BEGIN --dir=%s dry_run=%s" % (root, dry_run))
            device = os.fstat(rootfd).st_dev
            removed = [0, 0]
            failed = [False]

            def info(fd, name):
                try:
                    return os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    return None

            def remove(fd, name, relative):
                entry = info(fd, name)
                if entry is None:
                    return True
                if name == ".git" or entry.st_uid != os.getuid() or entry.st_dev != device:
                    note("KEEP %s (git/owner/device protection)" % relative)
                    return False
                if stat.S_ISDIR(entry.st_mode):
                    childfd = os.open(name, flags, dir_fd=fd)
                    try:
                        if info(childfd, ".git") is not None:
                            note("KEEP %s (reusable Git worktree)" % relative)
                            return False
                        if not dry_run:
                            os.fchmod(childfd, entry.st_mode | stat.S_IWUSR | stat.S_IXUSR)
                        empty = True
                        for child in os.listdir(childfd):
                            empty = remove(childfd, child, relative + "/" + child) and empty
                    finally:
                        os.close(childfd)
                    if empty:
                        if not dry_run:
                            os.rmdir(name, dir_fd=fd)
                        removed[0] += 1
                        removed[1] += entry.st_blocks * 512
                    return empty
                # unlink removes only the local directory entry, including a link to an outside tree.
                if not dry_run:
                    os.unlink(name, dir_fd=fd)
                removed[0] += 1
                removed[1] += entry.st_blocks * 512
                return True

            def discover(fd, prefix=""):
                for name in sorted(os.listdir(fd)):
                    relative = prefix + name
                    entry = info(fd, name)
                    if entry is None or name == ".git":
                        continue
                    if stat.S_ISLNK(entry.st_mode):
                        if name.startswith(("rw", "bt")) or name in ("build", "tmp"):
                            note("REFUSE %s (directory symlink; target not traversed)" % relative)
                        continue
                    if not stat.S_ISDIR(entry.st_mode):
                        continue
                    if entry.st_dev != device or entry.st_uid != os.getuid():
                        note("KEEP %s (owner/device protection)" % relative)
                        continue
                    try:
                        childfd = os.open(name, flags, dir_fd=fd)
                        try:
                            if info(childfd, ".git") is not None:
                                note("KEEP %s (reusable Git worktree)" % relative)
                                continue
                            config = info(childfd, "config.toml")
                            versions = info(childfd, "versions")
                            project = (
                                config is not None
                                and stat.S_ISREG(config.st_mode)
                                and versions is not None
                                and stat.S_ISDIR(versions.st_mode)
                            )
                            selected = name.startswith(("rw", "bt")) or name in ("build", "tmp") or project
                            if not selected:
                                discover(childfd, relative + "/")
                        finally:
                            os.close(childfd)
                        if selected:
                            note("%s %s" % ("WOULD REMOVE" if dry_run else "REMOVE", relative))
                            if not remove(fd, name, relative):
                                note("REMAINS %s (protected descendants)" % relative)
                    except OSError as error:
                        failed[0] = True
                        note("ERROR %s: %s" % (relative, error))

            discover(rootfd)
            note(
                "END entries=%d allocated_bytes=%d errors=%s dry_run=%s" % (removed[0], removed[1], failed[0], dry_run)
            )
            return not failed[0]
    except OSError as error:
        print("CLEANUP ERROR: %s: %s" % (root, error), flush=True)
        return False
    finally:
        os.close(rootfd)


def cleanup_command():
    parser = argparse.ArgumentParser(description="Inspect or clean an ended lane without launching Codex")
    parser.add_argument("--cleanup-only", action="store_true", required=True)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return 0 if cleanup_lane(args.dir, args.dry_run) else 1


# The stop rule --continues checks against RESULT.md (in --dir), named explicitly because the two kinds of
# lane disagree on what "done" means. A function-drafting lane's RESULT.md keeps saying it is still trying
# until it either matches (" identical ") or gives up, so anything else there means keep going. A
# tool/integration lane's RESULT.md is written once, when the work is finished, so any non-empty RESULT.md
# already means done -- checking for " identical " there told a finished integrator to keep going anyway,
# four times, because an integrator's RESULT.md never says that word. DONE_WHEN was refused by name rather
# than guessed because the two rules produce opposite answers on the same file and neither is a safe
# default for the other kind of lane.
DONE_WHEN = ("identical", "result")


class ContextCap:
    """One event-driven lifecycle for every model; running actions drain before a hard-cap interrupt."""

    prepare = 230000
    limit = 256000
    # Seconds a lane may take to finish after its HANDOFF.md is fresh and nothing is running.
    handoff_window = float(os.environ.get("LANE_HANDOFF_SECONDS") or 60)
    notice_template = (
        "CONTEXT HANDOFF: stop starting new work. Finish the current safe operation, preserve all "
        "work/proofs/backups, write HANDOFF.md with state paths, metrics, exact next action and open "
        "routes, then end this turn. Write HANDOFF.md in the lane output directory; never write RESULT.md "
        "for a context handoff. RESULT.md is reserved for the --done-when completion contract. "
        "If unfinished, remove any handoff-only RESULT.md after preserving its contents in HANDOFF.md. "
        "%s "
        "Do not compact or continue this thread. These lifecycle instructions supersede any "
        "older instructions to write RESULT.md on handoff or wait for the owner to relaunch."
    )

    def __init__(self):
        self.peak = 0
        self.turn = None
        self.active = set()
        self.warned = False
        self.interrupted = False
        self.handoff_before = None
        self.handoff_first_version = None
        self.handoff_first_observed_at = None
        self.handoff_first_eligible_at = None
        self.handoff_deadline = None
        self.handoff_deadline_at = None
        self.handoff_deadline_reported = False

    @property
    def requested(self):
        return self.peak >= self.prepare

    def observe(self, frame):
        method = frame.get("method")
        params = frame.get("params") or {}
        if method == "thread/tokenUsage/updated":
            value = ((params.get("tokenUsage") or {}).get("last") or {}).get("inputTokens")
            if type(value) is int and value >= 0:
                self.peak = max(self.peak, value)
        elif method == "turn/started":
            self.turn = (params.get("turn") or {}).get("id")
        elif method == "turn/completed":
            self.turn = None
            # An interrupted turn never emits item/completed for the items it aborted, and an item/started
            # can arrive after the interrupt was sent. Codex items cannot outlive their turn; real OS
            # writers are tracked by native_jobs, so nothing here may keep the lane waiting for a ghost.
            self.active.clear()
        elif method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            # Unknown tool/action kinds are conservatively treated as possible writers.
            if item.get("type") not in ("agentMessage", "userMessage", "reasoning", "plan") and item.get("id"):
                if method == "item/started":
                    if self.turn is None:
                        return  # a late start for a turn that already completed would never end
                    self.active.add(item["id"])
                else:
                    self.active.discard(item["id"])

    def actions(self, native_jobs=(), handoff_version=None):
        if not self.requested:
            return []
        fresh = handoff_version is not None and handoff_version != self.handoff_before and handoff_version[2] > 0
        if fresh and self.handoff_first_observed_at is None:
            self.handoff_first_version = handoff_version
            self.handoff_first_observed_at = time.time()
        if fresh and not self.active and not native_jobs and self.handoff_deadline is None:
            self.handoff_first_eligible_at = time.time()
            self.handoff_deadline = time.monotonic() + self.handoff_window
            self.handoff_deadline_at = self.handoff_first_eligible_at + self.handoff_window
        if not self.turn:
            return []
        actions = []
        if not self.warned:
            self.warned = True
            actions.append("steer")
        if (
            (self.peak >= self.limit or (fresh and self.handoff_deadline is not None))
            and not self.active
            and not native_jobs
            and not self.interrupted
        ):
            self.interrupted = True
            actions.append("interrupt")
        return actions


def is_done(done_when, said):
    """True once RESULT.md's text (`said`) means this lane's turn is finished, under DONE_WHEN's rule."""
    if done_when == "identical":
        return " identical " in said
    if done_when == "result":
        return said.strip() != ""
    raise ValueError("done_when must be one of %s, not %r" % (DONE_WHEN, done_when))


def handoff_only_result(said):
    """Reserved legacy handoff forms, not prose guessing or a new completion test.

    A sole HANDOFF.md pointer (optionally `RESULT: `) or a first line exactly
    CONTEXT HANDOFF / starting ContextHandoff: explicitly declares a handoff.
    All other RESULT.md text is passed unchanged to is_done, even if it mentions HANDOFF.
    New handoffs must write only HANDOFF.md; this bridge preserves old pointer results.
    """
    lines = said.strip().splitlines()
    if not lines:
        return False
    if lines[0] == "CONTEXT HANDOFF" or lines[0].startswith("ContextHandoff:"):
        return True
    return len(lines) == 1 and lines[0] in ("HANDOFF.md", "RESULT: HANDOFF.md")


def file_version(path):
    try:
        entry = os.stat(path)
        return (entry.st_ino, entry.st_mtime_ns, entry.st_size)
    except FileNotFoundError:
        return None


LADDER_FOR_TESTS = None
SELF_TEST_LADDER = """ladder = ["sol", "astra", "sonnet", "opus"]
[models]
"gpt-6-astra" = "astra"
sonnet = "sonnet"
[kind.tooling]
min = "sonnet"
max = "opus"
opus_needs_owner = true
[kind.impl]
min = "sol"
max = "opus"
opus_needs_owner = true
"""


def self_test():
    assert is_done("identical", "still trying, not there yet") is False
    assert is_done("identical", "byte-for-byte identical to the reference") is True
    assert is_done("result", "") is False
    assert is_done("result", "   \n") is False
    assert is_done("result", "OK: merged") is True
    assert handoff_only_result("CONTEXT HANDOFF\nsee HANDOFF.md")
    assert handoff_only_result("ContextHandoff: unfinished")
    assert handoff_only_result("HANDOFF.md")
    assert handoff_only_result("RESULT: HANDOFF.md")
    assert not handoff_only_result("OK: done; see HANDOFF.md")
    assert not handoff_only_result("Result includes a HANDOFF.md for documentation")
    here = os.path.abspath(__file__)
    test_root = os.environ.get("TMPDIR", os.path.dirname(os.path.abspath(__file__)))
    global LADDER_FOR_TESTS
    LADDER_FOR_TESTS = os.path.join(test_root, "lane-self-test-ladder.toml")
    with open(LADDER_FOR_TESTS, "w") as ladder_file:
        ladder_file.write(SELF_TEST_LADDER)
    base = [
        "--codex",
        "/nonexistent-codex",
        "--model",
        "sonnet",
        "--kind",
        "tooling",
        "--ladder",
        LADDER_FOR_TESTS,
        "--effort",
        "e",
        "--cwd",
        test_root,
        "--brief",
        "/nonexistent-brief",
        "--dir",
        os.path.join(test_root, "lane-self-test-missing"),
        "--socket",
        os.path.join(test_root, "lane-self-test.sock"),
        "--name",
        "n",
        "--wall",
        "10",
        "--fanout",
        "disabled",
        "--max-agents",
        "0",
    ]
    missing = subprocess.run([sys.executable, here, "--continues", "1"] + base, capture_output=True, text=True)
    assert missing.returncode != 0 and "DoneWhenMissing" in missing.stderr, missing.stderr
    invalid = subprocess.run(
        [sys.executable, here, "--continues", "1", "--done-when", "bogus"] + base, capture_output=True, text=True
    )
    assert invalid.returncode != 0 and "DoneWhenInvalid" in invalid.stderr, invalid.stderr
    ok = subprocess.run(
        [sys.executable, here, "--continues", "1", "--done-when", "result"] + base, capture_output=True, text=True
    )
    assert "DoneWhen" not in ok.stderr, ok.stderr
    with tempfile.TemporaryDirectory(prefix="lane-self-test-", dir=test_root) as temporary:
        from pathlib import Path

        work = Path(temporary)
        lane = work / "lane"
        lane.mkdir()
        outside = work / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("safe")
        for name in ("rw", "bt-trial", "tmp", "build", "nested/build", "custom/versions"):
            (lane / name).mkdir(parents=True)
            (lane / name / "output.o").write_text("discard")
        (lane / "custom/config.toml").write_text("project")
        (lane / "rw/.git").mkdir()
        (lane / "rw/.git/sentinel").write_text("git protected")
        (lane / "rw/outside").symlink_to(outside, target_is_directory=True)
        (lane / "rw-external").symlink_to(outside, target_is_directory=True)
        evidence = ("RESULT.md", "brief.md", "fix.patch", "fix.untracked", "proof.log", "data.json", "draft.c", "draft.h")
        for name in evidence:
            (lane / name).write_text("evidence")
        assert cleanup_lane(lane, dry_run=True)
        assert (lane / "bt-trial/output.o").exists()
        assert cleanup_lane(lane)
        assert not any((lane / name).exists() for name in ("bt-trial", "tmp", "build", "nested/build", "custom"))
        assert (lane / "rw/.git/sentinel").read_text() == "git protected"
        assert (lane / "rw/output.o").read_text() == "discard"
        assert (lane / "rw/outside").is_symlink()
        assert (outside / "sentinel").read_text() == "safe"
        assert all((lane / name).read_text() == "evidence" for name in evidence)
        assert (lane / "rw-external").is_symlink()
        assert cleanup_lane(lane)
        alias = work / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        assert cleanup_lane(alias) is False
        assert not (outside / "launch.log").exists()
        assert cleanup_lane("/") is False
        # An ordinary validation failure must also invoke the registered exit cleanup.
        (lane / "tmp").mkdir()
        (lane / "tmp/leak").write_text("discard")
        failed_base = list(base)
        failed_base[failed_base.index("--dir") + 1] = str(lane)
        failed = subprocess.run([sys.executable, here] + failed_base, capture_output=True, text=True)
        assert failed.returncode != 0 and "CodexMissing" in failed.stderr
        assert not (lane / "tmp").exists(), failed.stderr
        fake = work / "fake-codex"
        fake.write_text("#!%s\nimport time\ntime.sleep(60)\n" % sys.executable)
        fake.chmod(0o700)
        brief = work / "brief"
        brief.write_text("self-test")
        runtime = list(failed_base)
        for option, value in (("--codex", str(fake)), ("--brief", str(brief)), ("--wall", "1")):
            runtime[runtime.index(option) + 1] = value
        (lane / "tmp").mkdir()
        (lane / "tmp/leak").write_text("discard")
        wall = subprocess.run([sys.executable, here] + runtime, capture_output=True, text=True, timeout=20)
        assert wall.returncode == 6 and not (lane / "tmp").exists(), wall.stderr
        runtime[runtime.index("--wall") + 1] = "30"
        (lane / "server.pid").unlink()
        (lane / "tmp").mkdir()
        (lane / "tmp/leak").write_text("discard")
        terminated = subprocess.Popen([sys.executable, here] + runtime, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not (lane / "server.pid").exists() and terminated.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (lane / "server.pid").exists()
            terminated.terminate()
            output, errors = terminated.communicate(timeout=20)
            assert terminated.returncode == 128 + signal.SIGTERM and not (lane / "tmp").exists(), errors
        finally:
            if terminated.poll() is None:
                terminated.terminate()
                terminated.communicate(timeout=20)
        # Popen's own exception occurs before a server exists, but still triggers cleanup.
        fake.chmod(0o600)
        (lane / "tmp").mkdir()
        (lane / "tmp/leak").write_text("discard")
        launch_error = subprocess.run([sys.executable, here] + runtime, capture_output=True, text=True, timeout=20)
        assert launch_error.returncode != 0 and not (lane / "tmp").exists(), launch_error.stderr
    # All lifecycle scenarios run the production CLI and websocket code, with a local fake server.
    rollover_self_test(here, test_root)
    print(
        "OK(lane-self-test): stop rules, cleanup, evidence, .git, symlink containment, validation/launch errors, wall and SIGTERM passed"
    )


def rollover_self_test(here, test_root):
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="lr-", dir=test_root) as temporary:
        work = Path(temporary)
        fake = work / "fake-codex"
        fake.write_text(
            "#!%s\n" % sys.executable
            + r"""
import json, os, socket, struct, subprocess, sys, threading, time
from pathlib import Path
root = Path.cwd()
scenario = (root / 'scenario').read_text()
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(sys.argv[sys.argv.index('--listen')+1].removeprefix('unix://'))
listener.listen()
owner = None
threads = turns = 0
current = active = None
lock = threading.RLock()

def audit(frame):
    with open(root / 'requests.jsonl', 'a') as output: output.write(json.dumps(frame)+'\n')

class Client:
    def __init__(self, connection):
        self.sock = connection; self.buf = b''; self.lock = threading.RLock()
    def exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk: raise EOFError()
            self.buf += chunk
        result, self.buf = self.buf[:n], self.buf[n:]
        return result
    def send(self, value):
        payload = json.dumps(value).encode(); n = len(payload)
        header = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126])+struct.pack('>H', n) if n < 65536 else bytes([127])+struct.pack('>Q', n))
        with self.lock: self.sock.sendall(header + payload)
    def recv(self):
        b0, b1 = self.exact(2); n = b1 & 127
        if n == 126: n = struct.unpack('>H', self.exact(2))[0]
        elif n == 127: n = struct.unpack('>Q', self.exact(8))[0]
        mask = self.exact(4) if b1 & 128 else None
        data = self.exact(n)
        if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return json.loads(data)

def event(method, **params):
    params.setdefault('threadId', current)
    owner.send({'method': method, 'params': params})

def finish(text='final', status='completed'):
    event('item/completed', item={'id': 'answer', 'type': 'agentMessage', 'phase': 'final_answer', 'text': text})
    event('turn/completed', turn={'id': active, 'status': status})

def native_job():
    # Real detached OS work, no outstanding tool item. The production subreaper/pidfd drain must wait.
    subprocess.Popen([sys.executable, '-c', "import time; from pathlib import Path; time.sleep(1.2); Path('native-finished').write_text('drained')"], start_new_session=True)

def process(client, frame):
    global threads, turns, current, active, owner
    audit(frame)
    method, params, rid = frame.get('method'), frame.get('params') or {}, frame.get('id')
    if method == 'initialize':
        if owner is None: owner = client
        client.send({'id': rid, 'result': {}})
    elif method == 'account/rateLimits/read': client.send({'id': rid, 'result': {}})
    elif method in ('thread/start', 'thread/resume'):
        threads += 1
        if threads == 2 and scenario == 'failed':
            client.send({'id': rid, 'error': {'code': -1, 'message': 'injected thread start failure'}}); return
        if threads == 2 and scenario == 'silent': return
        if threads == 2 and scenario == 'disconnected':
            client.sock.close(); raise EOFError()
        if threads == 2 and scenario == 'bad-id':
            client.send({'id': rid, 'result': {'thread': {'id': current}}}); return
        current = params.get('threadId') or 'thread-%d' % threads
        client.send({'id': rid, 'result': {'thread': {'id': current}, 'model': params.get('model')}})
    elif method == 'thread/name/set': client.send({'id': rid, 'result': {}})
    elif method == 'turn/start':
        turns += 1; active = 'turn-%d' % turns
        client.send({'id': rid, 'result': {'turn': {'id': active}}})
        event('turn/started', turn={'id': active})
        (root / 'active').write_text(current + ' ' + active)
        if scenario in ('wall', 'wall-before') and current == 'thread-1': time.sleep(0.65)
        capped = (current == 'thread-1' and scenario not in ('native', 'cancel', 'plain')) or (scenario == 'double' and current == 'thread-2')
        if scenario == 'limit' and turns == 1: finish('not finished'); return
        if scenario in ('steer', 'wall', 'cancel') and (current != 'thread-1' or scenario == 'cancel'): return
        if capped:
            if scenario == 'real-completion':
                (root / 'RESULT.md').write_text('Completed task; notes in HANDOFF.md')
            if scenario in ('hard', 'nativecap'):
                event('item/started', item={'type': 'commandExecution', 'id': 'writer'})
            event('thread/tokenUsage/updated', tokenUsage={'last': {'inputTokens': 256000 if scenario in ('hard', 'nativecap') else 230001}})
        else:
            if scenario == 'native': native_job()
            if scenario not in ('limit',): (root / 'RESULT.md').write_text('OK: task complete')
            if scenario == 'identical': (root / 'RESULT.md').write_text('function identical to reference')
            # Old-thread updates queued during rollover cannot trigger a new cap.
            if current != 'thread-1':
                owner.send({'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread-1', 'tokenUsage': {'last': {'inputTokens': 256000}}}})
            finish()
    elif method == 'turn/steer':
        if params.get('threadId') != current or params.get('expectedTurnId') != active:
            client.send({'id': rid, 'error': {'code': -1, 'message': 'wrong steer target'}}); return
        instruction = params['input'][0]['text']
        if instruction.startswith('CONTEXT HANDOFF:'):
            if scenario not in ('missing', 'stale'):
                (root / 'HANDOFF.md').write_text('unfinished; state=' + current + '; next: continue exact task')
            if scenario == 'legacy': (root / 'RESULT.md').write_text('CONTEXT HANDOFF\nunfinished; see HANDOFF.md')
            if scenario == 'pointer': (root / 'RESULT.md').write_text('HANDOFF.md')
            if scenario == 'identical': (root / 'RESULT.md').write_text('function not-ready: keep going')
            client.send({'id': rid, 'result': {}})
            if scenario in ('nativecap', 'wall-before'): native_job()
            if scenario in ('hard', 'nativecap'):
                event('item/completed', item={'type': 'commandExecution', 'id': 'writer'})
            elif scenario in ('ghost', 'ghost-hang'): pass
            else: finish('handoff')
        else:
            client.send({'id': rid, 'result': {}})
            (root / 'steer-delivered').write_text(current + ': ' + instruction)
            (root / 'RESULT.md').write_text('OK: steered successor complete')
            finish()
    elif method == 'turn/interrupt':
        client.send({'id': rid, 'result': {}})
        if scenario in ('ghost', 'ghost-hang'):
            # Real codex: the aborted exec's start is reported after the interrupt and its end never is.
            event('item/started', item={'type': 'commandExecution', 'id': 'exec-ghost'})
        if scenario != 'ghost-hang': finish('handoff', 'interrupted')
    elif rid is not None: client.send({'id': rid, 'result': {}})

def serve(connection):
    client = Client(connection)
    try:
        while b'\r\n\r\n' not in client.buf: client.buf += connection.recv(4096)
        _, client.buf = client.buf.split(b'\r\n\r\n', 1)
        connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
        while True:
            frame = client.recv()
            with lock: process(client, frame)
    except (EOFError, ConnectionError): pass

while True:
    connection, _ = listener.accept()
    threading.Thread(target=serve, args=(connection,), daemon=True).start()
"""
        )
        fake.chmod(0o700)

        def launch(scenario, wall=10, continues=0, done_when="result", rollover="1", name=None, window=None):
            directory = work / (name or scenario)
            directory.mkdir()
            (directory / "scenario").write_text(scenario)
            (directory / "brief.md").write_text("ORIGINAL TASK: finish the work")
            if scenario == "stale":
                (directory / "HANDOFF.md").write_text("old handoff")
            (directory / "build").mkdir()
            (directory / "build/evidence").write_text("warm artifact")
            command = [
                sys.executable,
                here,
                "--codex",
                str(fake),
                "--model",
                "gpt-6-astra",
                "--kind",
                "impl",
                "--ladder",
                LADDER_FOR_TESTS,
                "--effort",
                "low",
                "--cwd",
                str(directory),
                "--brief",
                str(directory / "brief.md"),
                "--dir",
                str(directory),
                "--socket",
                str(directory / "s"),
                "--name",
                scenario,
                "--wall",
                str(wall),
                "--fanout",
                "disabled",
                "--max-agents",
                "0",
                "--continues",
                str(continues),
                "--done-when",
                done_when,
            ]
            env = dict(os.environ, LANE_ROLLOVER=rollover)
            if window is not None:
                env["LANE_HANDOFF_SECONDS"] = str(window)
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            return directory, process

        def complete(directory, process, expected=0):
            try:
                output, errors = process.communicate(timeout=40 if directory.name == "silent" else 15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.communicate(timeout=10)
                raise
            assert process.returncode == expected, (directory.name, process.returncode, errors)
            requests = [json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()]
            terminal = json.loads((directory / "LIFECYCLE-STATE.json").read_text())
            assert terminal["terminal"] is True and terminal["exit"] == expected
            assert not (directory / "physical-exit-persistent.json").exists()
            return requests, terminal, errors

        # Without LANE_ROLLOVER a lane of any model closes at its first cap; the orchestrator relaunches it.
        directory, process = launch("rollover", rollover="0", name="closes-at-cap")
        requests, terminal, errors = complete(directory, process, 9)
        assert "ContextCap: closed at first cap" in errors and terminal["status"] == "unfinished"
        assert sum(frame.get("method") == "thread/start" for frame in requests) == 1
        print("OK(lane-rollover): non-Sol lane closes at first cap without LANE_ROLLOVER")

        for scenario in ("rollover", "legacy", "pointer", "hard", "nativecap", "double", "identical", "ghost"):
            directory, process = launch(scenario, done_when="identical" if scenario == "identical" else "result")
            requests, terminal, errors = complete(directory, process)
            starts = [frame for frame in requests if frame.get("method") == "thread/start"]
            records = [json.loads(line) for line in (directory / "ROLLOVERS.jsonl").read_text().splitlines()]
            expected = 2 if scenario == "double" else 1
            assert len(starts) == expected + 1 and len(records) == expected
            assert records[0]["old_thread_id"] == "thread-1" and records[0]["new_thread_id"] == "thread-2"
            assert records[0]["input_tokens_peak"] >= 230000
            assert (directory / "thread.id").read_text() == "thread-%d" % (expected + 1)
            turns = [frame["params"] for frame in requests if frame.get("method") == "turn/start"]
            for index in range(1, expected + 1):
                prompt = turns[index]["input"][0]["text"]
                assert "ORIGINAL TASK" in prompt and "HANDOFF.md contents:" in prompt
                assert "unfinished; state=thread-%d" % index in prompt
                assert prompt.endswith("RESUME %d: continue from HANDOFF" % index)
                assert "never write RESULT.md" in prompt and "Lane output directory:" in prompt
                assert "CONDITIONAL CONTEXT POLICY, not an active handoff request" in prompt
                assert "CONTEXT HANDOFF: stop starting" not in prompt
            assert terminal["status"] == "completed" and terminal["turn_status"] == "completed"
            context = json.loads((directory / "CONTEXT-LIMIT.json").read_text())
            assert context["terminal"] is True and context["work_state"] == "completed"
            assert "ROLLOVER 1" in (directory / "STATUS.md").read_text()
            assert (directory / "build/evidence").read_text() == "warm artifact"
            if scenario in ("legacy", "pointer"):
                assert (directory / "HANDOFF-RESULT-1.txt").exists()
            if scenario in ("hard", "nativecap"):
                assert any(frame.get("method") == "turn/interrupt" for frame in requests)
            if scenario == "nativecap":
                assert (directory / "native-finished").read_text() == "drained"
            print("OK(lane-rollover): " + scenario)

        directory, process = launch("ghost-hang", window=1)
        requests, terminal, errors = complete(directory, process, 8)
        assert "no live process for active actions" in errors and terminal["status"] == "unfinished"
        assert terminal["active_actions"] == ["exec-ghost"] and (directory / "HANDOFF.md").exists()
        print("OK(lane-rollover): lost exec end with no live process ends the lane with exit 8, not a hang")

        directory, process = launch("steer")
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if (directory / "active").exists() and (directory / "active").read_text().startswith("thread-2 "):
                    break
                assert process.poll() is None
                time.sleep(0.02)
            assert (directory / "thread.id").read_text() == "thread-2"
            assert not (directory / "LIFECYCLE-STATE.json").exists()
            context = json.loads((directory / "CONTEXT-LIMIT.json").read_text())
            assert context["state"] == "rollover_running" and context["terminal"] is False
            instruction = directory / "steer.txt"
            instruction.write_text("finish after rollover")
            steered = subprocess.run(
                [
                    sys.executable,
                    str(Path(here).with_name("steer.py")),
                    str(directory),
                    str(directory / "s"),
                    str(instruction),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert steered.returncode == 0, steered.stderr + steered.stdout
            requests, terminal, errors = complete(directory, process)
            assert (directory / "steer-delivered").read_text() == "thread-2: finish after rollover"
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
        print("OK(lane-rollover): steer.py reaches successor on same socket")

        directory, process = launch("real-completion", continues=3)
        requests, terminal, errors = complete(directory, process)
        assert len([f for f in requests if f.get("method") == "thread/start"]) == 1
        assert not (directory / "ROLLOVERS.jsonl").exists() and terminal["status"] == "completed"
        print("OK(lane-rollover): real RESULT.md completion at cap stops without rollover")

        directory, process = launch("limit", continues=1)
        requests, terminal, errors = complete(directory, process)
        assert len([f for f in requests if f.get("method") == "turn/start"]) == 3
        assert terminal["rollovers"] == 1 and terminal["status"] == "unfinished"
        assert terminal["turn_status"] == "completed"
        print("OK(lane-rollover): --continues budget shared across rollover; unfinished distinct from turn completed")

        for scenario in ("wall", "wall-before"):
            started = time.monotonic()
            directory, process = launch(scenario, wall=1)
            requests, terminal, errors = complete(directory, process, 6)
            elapsed = time.monotonic() - started
            if scenario == "wall":
                assert elapsed < 1.5, errors
                assert terminal["rollovers"] == 1
            else:
                assert (directory / "native-finished").read_text() == "drained"
                assert elapsed >= 1.5 and terminal["rollovers"] == 0
            assert terminal["status"] == "unfinished"
            assert "WallReached" in errors
        print("OK(lane-rollover): wall deadline retained across rollover")

        for scenario in ("failed", "bad-id", "missing", "stale", "disconnected", "silent"):
            directory, process = launch(scenario, wall=45 if scenario == "silent" else 10)
            requests, terminal, errors = complete(directory, process, 8)
            assert "RolloverFailed" in errors and terminal["status"] == "unfinished"
            assert (directory / "build/evidence").read_text() == "warm artifact"
            context = json.loads((directory / "CONTEXT-LIMIT.json").read_text())
            assert context["terminal"] is True and context["state"] == "stopped_for_handoff"
        print(
            "OK(lane-rollover): failed thread start/id, missing/stale HANDOFF, disconnect and RPC timeout fail explicitly with preserved state"
        )

        directory, process = launch("plain")
        requests, terminal, errors = complete(directory, process)
        assert not any(f.get("method") in ("turn/steer", "turn/interrupt") for f in requests)
        prompt = next(f["params"]["input"][0]["text"] for f in requests if f.get("method") == "turn/start")
        assert "CONDITIONAL CONTEXT POLICY, not an active handoff request" in prompt
        assert "CONTEXT HANDOFF: stop starting" not in prompt
        assert not (directory / "HANDOFF.md").exists() and terminal["status"] == "completed"
        print("OK(lane-rollover): fresh small context continues normally; cap policy explicitly conditional")

        directory, process = launch("native")
        requests, terminal, errors = complete(directory, process)
        assert (directory / "native-finished").read_text() == "drained" and not terminal["active_jobs"]
        assert not (directory / "build").exists()
        print("OK(lane-rollover): real detached native job drained before completion cleanup")

        directory, process = launch("cancel")
        deadline = time.monotonic() + 10
        while not (directory / "active").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (directory / "active").exists()
        process.terminate()
        requests, terminal, errors = complete(directory, process, 128 + signal.SIGTERM)
        assert terminal["status"] == "cancelled"
        print("OK(lane-rollover): cancelled terminal state distinct from unfinished/completed")


if sys.argv[1:2] == ["--self-test"]:
    self_test()
    sys.exit(0)

if "--cleanup-only" in sys.argv[1:]:
    sys.exit(cleanup_command())

ap = argparse.ArgumentParser()
for k in ("codex", "model", "effort", "cwd", "brief", "dir", "socket", "name", "wall"):
    ap.add_argument("--" + k, required=True)
ap.add_argument("--fanout", required=True, choices=("enabled", "disabled"))
ap.add_argument("--max-agents", required=True, type=int)
# --continues N: after a completed turn whose RESULT.md (in --dir) is not yet done by --done-when's rule,
# send up to N more turns telling the lane to keep going, while the wall allows. Lanes otherwise stop on
# a plateau with most of their time unused.
# A running pool built its lane command lines before this flag existed, so absence means one turn, as before.
ap.add_argument("--continues", type=int)
ap.add_argument("--done-when")
ap.add_argument("--resume-thread", help="Explicit existing thread id to resume instead of starting a new thread")
# --kind tooling|impl is checked against the owner's model ladder (--ladder). --owner-override REASON
# records an explicit owner choice outside the ladder (e.g. an Astra tooling lane the owner picked).
ap.add_argument("--kind", required=True, choices=("tooling", "impl"))
ap.add_argument("--owner-override")
ap.add_argument("--ladder", required=True, help="the owner model ladder file (models.toml)")
# --owns PATH (repeatable) and --track are recorded in LAUNCH.json for `decomp lane continue`.
ap.add_argument("--owns", action="append")
# --export PATH (repeatable): a file under an owned dir that carries this lane's work instead of commits; a link whose
# export changed (newer than the link start, other hash) progressed and keeps its --continues budget.
ap.add_argument("--export", action="append")
ap.add_argument("--track", choices=("official", "experiment"))
# --profile: the seat profile `decomp lane continue` runs with when this lane closes (absent: no automatic continuation).
ap.add_argument("--profile")
a = ap.parse_args()
from abu_decomp import DecompError
from abu_decomp.lanes import Ladder

try:
    Ladder.load(__import__("pathlib").Path(a.ladder)).check(a.kind, a.model, a.owner_override)
except DecompError as error:
    sys.exit("ModelPolicy: %s; pass --owner-override REASON only for an explicit owner choice" % error.detail)
context_cap = ContextCap()
if (a.fanout == "enabled" and a.max_agents <= 0) or (a.fanout == "disabled" and a.max_agents != 0):
    sys.exit("FanoutInvalid: enabled requires positive --max-agents; disabled requires 0")
if a.resume_thread is not None and not a.resume_thread.strip():
    sys.exit("ResumeThreadMissing: --resume-thread requires a nonempty explicit thread id")
# Register before validation and server launch, covering failures, normal completion and exceptions.
server = None
preserve_state = False


def finish_lane():
    signal.alarm(0)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    if server is not None:
        try:
            os.killpg(server.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(server.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            server.wait()
        try:
            os.unlink(a.socket)
        except FileNotFoundError:
            pass
    # Lifecycle recycling retains warm artifacts, proof state and all backups for the successor.
    if os.path.isdir(a.dir) and not (context_cap.requested or preserve_state):
        cleanup_lane(a.dir)


atexit.register(finish_lane)


def cancel_lane(received, _frame):
    if "stop" in globals():
        stop(128 + received, "LaneCancelled: signal %d" % received, cancelled=True)
    sys.exit(128 + received)


for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, cancel_lane)
if not a.wall.isdigit() or int(a.wall) <= 0:
    sys.exit("WallInvalid: " + a.wall)
if a.continues is not None and a.continues < 0:
    sys.exit("ContinuesInvalid: --continues must be nonnegative")
if a.continues and a.done_when is None:
    sys.exit("DoneWhenMissing: --done-when (%s) is required when --continues is given" % "|".join(DONE_WHEN))
if a.done_when is not None and a.done_when not in DONE_WHEN:
    sys.exit("DoneWhenInvalid: %s (%s)" % (a.done_when, "|".join(DONE_WHEN)))
for k in ("codex", "brief"):
    if not os.path.isfile(getattr(a, k)):
        sys.exit("%sMissing: %s" % (k.capitalize(), getattr(a, k)))
if not os.path.isdir(a.cwd):
    sys.exit("CwdMissing: " + a.cwd)
os.makedirs(a.dir, exist_ok=True)
# LAUNCH.json: every parsed launch setting, so a successor lane can be launched as this one was after its unit is gone.
with open(os.path.join(a.dir, "LAUNCH.json.new"), "w") as launch_file:
    json.dump({"schema": 1, "argv": sys.argv[1:], **vars(a)}, launch_file, indent=1, sort_keys=True)
os.replace(os.path.join(a.dir, "LAUNCH.json.new"), os.path.join(a.dir, "LAUNCH.json"))
if os.path.exists(a.socket):
    sys.exit("SocketInUse: " + a.socket)
terminal_path = os.path.join(a.dir, "LIFECYCLE-STATE.json")
if os.path.exists(terminal_path):
    os.replace(terminal_path, terminal_path + ".previous-%d" % time.time_ns())
STARTED = time.time()  # this link's start: commits after it count as its progress
EXPORTS_BEFORE = __import__("abu_decomp.lanes.relaunch", fromlist=["x"]).export_hashes(a.export or [])
log = open(os.path.join(a.dir, "transcript.jsonl"), "a", buffering=1)


def record(direction, value):
    log.write(json.dumps({"at": time.time(), "direction": direction, "value": value}) + "\n")


fanout_value = "true" if a.fanout == "enabled" else "false"
fanout_config = ["-c", "features.multi_agent=" + fanout_value, "-c", "agents.enabled=" + fanout_value]
if a.fanout == "enabled":
    fanout_config += ["-c", "agents.max_concurrent_threads_per_session=" + str(a.max_agents)]
    fanout_config += [
        "-c",
        "agents.default_subagent_model=" + json.dumps(a.model),
        "-c",
        "agents.default_subagent_reasoning_effort=" + json.dumps(a.effort),
    ]
cmd = [
    a.codex,
    "-c",
    'approval_policy="never"',
    "-c",
    'sandbox_mode="danger-full-access"',
    *fanout_config,
    "-c",
    "features.hooks=false",
    "-c",
    'model_reasoning_effort="%s"' % a.effort,
    "app-server",
    "--listen",
    "unix://" + a.socket,
]
record("launch", {"command": cmd, "cwd": a.cwd, "model": a.model, "name": a.name, "pid": os.getpid()})
# Detached unified-session workers can outlive their tool item and original parent. Adopt orphaned
# descendants so the same lifecycle owner can drain them rather than killing them on RESULT.md/exit.
if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "cannot establish native-job lifecycle ownership")
server = subprocess.Popen(
    cmd,
    cwd=a.cwd,
    stdout=open(os.path.join(a.dir, "server.out"), "ab"),
    stderr=open(os.path.join(a.dir, "server.err"), "ab"),
    start_new_session=True,
)
open(os.path.join(a.dir, "server.pid"), "w").write(str(server.pid))


def stop(code, why, cancelled=False):
    # Intent to terminate, not proof of physical exit. watch_lane_exit owns that proof.
    if "completion" in globals():
        completed = completion()
    else:
        completed = False
    terminal = {
        "schema": 1,
        "at": time.time(),
        "terminal": True,
        "lane": a.name,
        "thread_id": globals().get("thread"),
        "exit": code,
        "why": why,
        "status": "cancelled" if cancelled else "completed" if completed else "unfinished",
        "turn_status": (globals().get("done", {}).get("turn") or {}).get("status"),
        "rollovers": globals().get("resumes", 0),
        "context_requested": context_cap.requested,
        "input_tokens_peak": context_cap.peak,
        "active_actions": sorted(context_cap.active),
        "active_jobs": native_jobs() if "native_jobs" in globals() else [],
        "handoff": os.path.isfile(os.path.join(a.dir, "HANDOFF.md")),
        "result": os.path.isfile(os.path.join(a.dir, "RESULT.md")),
    }
    destination = os.path.join(a.dir, "LIFECYCLE-STATE.json")
    with open(destination + ".new", "w") as output:
        json.dump(terminal, output, sort_keys=True)
        output.write("\n")
    os.replace(destination + ".new", destination)
    if context_cap.requested or preserve_state:
        context_notice("stopped_for_handoff", why, terminal=True, work_state=terminal["status"])
    if code:
        print(why, file=sys.stderr, flush=True)
    record("end", {"exit": code, "why": why})
    terminal["rig_jobs"] = record_rig_jobs()
    continue_lane(terminal)
    try:
        os.killpg(server.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        os.unlink(a.socket)
    except FileNotFoundError:
        pass
    sys.exit(code)


def record_rig_jobs():
    """The ssh jobs this link started and that still run, written to JOBS.json and HANDOFF.md for the successor."""
    if "native_jobs" not in globals():
        return 0
    from pathlib import Path

    from abu_decomp.lanes import jobs as lane_jobs

    try:
        found = lane_jobs.record(Path(a.dir), native_jobs(), owns=a.owns or [])
    except Exception as error:  # closing must not fail on the record
        print("RigJobsNotRecorded: %s" % error, file=sys.stderr, flush=True)
        return 0
    if found:
        record("lifecycle", {"state": "rig_jobs_recorded", "jobs": found, "at": time.time()})
    return len(found)


def continue_lane(terminal):
    """Continue this lane when it closed on an unfinished RESULT.md or a context handoff (abu_decomp.lanes.relaunch)."""
    if not a.profile:
        return
    from pathlib import Path

    from abu_decomp.config import load_profile
    from abu_decomp.lanes import control, relaunch

    directory = Path(a.dir)
    said = relaunch.result_text(directory)
    progressed = relaunch.progress(said, a.owns or [], STARTED, a.export or [], EXPORTS_BEFORE)
    action, why = relaunch.plan(
        a.done_when or "result",
        a.continues or 0,
        said,
        terminal["handoff"],
        terminal["context_requested"],
        terminal["status"] == "cancelled",
        progressed,
        terminal.get("rig_jobs", 0),
    )
    if action == "none":
        return
    row = {"name": a.name, "kind": a.kind, "model": a.model, "track": a.track}
    profile = None
    try:
        profile = load_profile(Path(a.profile))
        if action == "end":
            control.announce(profile, row, "passed", "%s ended without a continuation: %s" % (a.name, why))
            return
        if action == "blocked":
            control.announce(profile, row, "blocked", "%s not continued: %s" % (a.name, why))
            return
        successor = relaunch.next_name(a.name)
        unit = relaunch.spawn(
            Path(a.profile),
            directory,
            successor,
            relaunch.next_budget(a.continues or 0, progressed, terminal.get("rig_jobs", 0)),
        )
        control.announce(profile, row, "info", "%s continues as %s (%s) via %s" % (a.name, successor, why, unit))
    except Exception as error:  # the lane is closing; a failed continuation must not hide how it closed
        print("ContinuationFailed: %s" % error, file=sys.stderr, flush=True)
        if profile is not None:
            try:
                control.announce(profile, row, "failed", "%s not continued: %s" % (a.name, error))
            except Exception:
                pass


signal.signal(signal.SIGALRM, lambda *_: stop(6, "WallReached: %s s" % a.wall))
wall_deadline = time.monotonic() + int(a.wall)
signal.alarm(int(a.wall))
deadline = time.time() + 60
while not os.path.exists(a.socket):
    if server.poll() is not None:
        stop(2, "ServerExited: %s" % server.returncode)
    if time.time() > deadline:
        stop(2, "SocketTimeout: " + a.socket)
    time.sleep(0.05)

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(a.socket)
key = base64.b64encode(os.urandom(16)).decode()
sock.sendall(
    (
        "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % key
    ).encode()
)
buf = b""
while b"\r\n\r\n" not in buf:
    chunk = sock.recv(4096)
    if not chunk:
        stop(2, "HandshakeClosed")
    buf += chunk
head, buf = buf.split(b"\r\n\r\n", 1)
rpc_deadline = None
if b" 101 " not in head.split(b"\r\n")[0]:
    stop(2, "HandshakeRefused: " + head.decode(errors="replace"))


def recv_exact(n):
    global buf
    while len(buf) < n:
        if rpc_deadline is not None:
            remaining = rpc_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("rollover RPC deadline reached")
            sock.settimeout(remaining)
        chunk = sock.recv(65536)
        if not chunk:
            stop(
                8 if rpc_deadline is not None else 3,
                "RolloverFailed: SocketClosed" if rpc_deadline is not None else "SocketClosed",
            )
        buf += chunk
    out, buf = buf[:n], buf[n:]
    return out


def send_frame(op, payload):
    mask = os.urandom(4)
    n = len(payload)
    hdr = bytes([0x80 | op]) + (
        bytes([0x80 | n])
        if n < 126
        else bytes([0x80 | 126]) + struct.pack(">H", n)
        if n < 65536
        else bytes([0x80 | 127]) + struct.pack(">Q", n)
    )
    sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def recv_message():
    data = b""
    op0 = None
    while True:
        b0, b1 = recv_exact(2)
        op = b0 & 0x0F
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", recv_exact(8))[0]
        mk = recv_exact(4) if b1 & 0x80 else None
        payload = recv_exact(n)
        if mk:
            payload = bytes(b ^ mk[i % 4] for i, b in enumerate(payload))
        if op == 0x9:
            send_frame(0xA, payload)
            continue
        if op == 0xA:
            continue
        if op == 0x8:
            stop(
                8 if rpc_deadline is not None else 3,
                "RolloverFailed: ServerClosedSocket" if rpc_deadline is not None else "ServerClosedSocket",
            )
        if op0 is None:
            op0 = op
        data += payload
        if b0 & 0x80:
            return json.loads(data)


def send(frame):
    record("out", frame)
    send_frame(0x1, json.dumps(frame).encode())


DECLINE = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "mcpServer/elicitation/request": {"action": "decline"},
}
final = {"text": None}
done = {"turn": None}
serial = [0]
overloaded = [False]
thread = None
context_calls = {}


def native_jobs():
    """Census owned OS descendants, including adopted orphans; never infer completion from items."""
    rows = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/" + name + "/stat") as source:
                fields = source.read().rsplit(")", 1)[1].split()
            rows[int(name)] = (int(fields[1]), fields[0])
        except (OSError, ValueError, IndexError):
            continue
    owned = {os.getpid()}
    while True:
        children = {pid for pid, (parent, _) in rows.items() if parent in owned} - owned
        if not children:
            break
        owned.update(children)
    runtime = {
        os.path.realpath(a.codex),
        os.path.join(os.path.dirname(os.path.realpath(a.codex)), "codex-code-mode-host"),
    }
    jobs = []
    for pid in sorted(owned - {os.getpid(), server.pid}):
        if rows[pid][1] == "Z":
            if rows[pid][0] == os.getpid():
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            continue
        try:
            executable = os.readlink("/proc/%d/exe" % pid)
        except OSError:
            executable = None
        if executable not in runtime:
            jobs.append(pid)
    return jobs


def next_step():
    if os.environ.get("LANE_ROLLOVER") == "1":
        return "automatic fresh thread in this lane from HANDOFF.md"
    return "closed at cap; orchestrator relaunches fresh from HANDOFF.md"


def successor_text():
    if os.environ.get("LANE_ROLLOVER") == "1":
        return "The runner will start a fresh thread automatically in this same lane and socket."
    return "This lane closes at the cap after you write HANDOFF.md and the orchestrator relaunches it fresh."


def cap_notice():
    return ContextCap.notice_template % successor_text()


def context_notice(state, error=None, terminal=False, work_state="unfinished"):
    document = {
        "schema": 1,
        "state": state,
        "thread_id": thread,
        "lane": a.name,
        "terminal": terminal,
        "work_state": work_state,
        "input_tokens_peak": context_cap.peak,
        "prepare_tokens": context_cap.prepare,
        "limit_tokens": context_cap.limit,
        "active_actions": sorted(context_cap.active),
        "native_pids": native_jobs(),
        "handoff": os.path.isfile(os.path.join(a.dir, "HANDOFF.md")),
        "result": os.path.isfile(os.path.join(a.dir, "RESULT.md")),
        "next": next_step(),
        "at": time.time(),
    }
    document.update(
        handoff_first_observed_at=context_cap.handoff_first_observed_at,
        handoff_first_eligible_at=context_cap.handoff_first_eligible_at,
        handoff_deadline_at=context_cap.handoff_deadline_at,
    )
    if error is not None:
        document["error"] = error
    destination = os.path.join(a.dir, "CONTEXT-LIMIT.json")
    temporary = destination + ".new"
    with open(temporary, "w") as output:
        json.dump(document, output, sort_keys=True)
        output.write("\n")
    os.replace(temporary, destination)
    record("lifecycle", document)
    print(json.dumps(document, sort_keys=True), file=sys.stderr, flush=True)


_auto_pulse_last = [0.0]


def auto_pulse(directory, method, now=None, interval=60, stale=300):
    """Write an automatic PULSE.json when the model's own pulse is missing or older than `stale` seconds."""
    try:
        import datetime

        now = time.time() if now is None else now
        if now - _auto_pulse_last[0] < interval:
            return False
        _auto_pulse_last[0] = now
        path = os.path.join(directory, "PULSE.json")
        old = {}
        try:
            with open(path) as f:
                old = json.load(f)
            age = now - datetime.datetime.fromisoformat(old["ts"]).timestamp()
            if age <= stale:
                return False
        except (OSError, ValueError, KeyError, TypeError):
            pass
        words = " ".join(str(method or "").split()[:2])
        obj = dict(
            ts=datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
            phase=("auto: working " + words).strip(),
            done=old.get("done", 0),
            total=old.get("total", 0),
            unit=old.get("unit", "units"),
            bytes_done=old.get("bytes_done", 0),
            commits=old.get("commits", 0),
            blocked="",
        )
        fd, tmp = tempfile.mkstemp(prefix=".PULSE-", dir=directory)
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def handle(frame):
    record("in", frame)
    # Late notifications from a retired thread must not contaminate its successor.
    source_thread = (frame.get("params") or {}).get("threadId")
    if source_thread is not None and thread is not None and source_thread != thread:
        return
    context_cap.observe(frame)
    auto_pulse(a.dir, frame.get("method"))
    m = frame.get("method")
    if not m and str(frame.get("id")) in context_calls:
        action = context_calls.pop(str(frame["id"]))
        context_notice(action + ("_refused" if frame.get("error") else "_accepted"), frame.get("error"))
    if m and "id" in frame:  # a server request: never approve, never leave it waiting
        r = DECLINE.get(m)
        send(
            {"id": frame["id"], "result": r}
            if r
            else {"id": frame["id"], "error": {"code": -32001, "message": "unattended lane: " + m}}
        )
    elif m == "item/completed":
        it = frame.get("params", {}).get("item", {})
        if it.get("type") == "agentMessage" and it.get("phase") == "final_answer":
            final["text"] = it.get("text")
    elif m == "error" and (frame.get("params", {}).get("error") or {}).get("codexErrorInfo") == "usageLimitExceeded":
        stop(7, "UsageLimitExceeded: " + (frame["params"]["error"].get("message") or ""))
    elif m == "error" and (frame.get("params", {}).get("error") or {}).get("codexErrorInfo") == "serverOverloaded":
        overloaded[0] = True
    elif m == "turn/completed":
        done["turn"] = frame.get("params", {}).get("turn", {})
    context_signals()


def context_signals(jobs=None):
    if thread is not None:
        if jobs is None:
            jobs = native_jobs() if context_cap.requested else ()
        observed, eligible = context_cap.handoff_first_observed_at, context_cap.handoff_first_eligible_at
        version = file_version(os.path.join(a.dir, "HANDOFF.md")) if context_cap.requested else None
        actions = context_cap.actions(jobs, version)
        if observed is None and context_cap.handoff_first_observed_at is not None:
            context_notice("handoff_observed")
        if eligible is None and context_cap.handoff_first_eligible_at is not None:
            context_notice("handoff_eligible")
        for action in actions:
            serial[0] += 1
            rid = str(serial[0])
            context_calls[rid] = action
            params = {"threadId": thread}
            if action == "steer":
                params.update(expectedTurnId=context_cap.turn, input=[{"type": "text", "text": cap_notice()}])
            else:
                params["turnId"] = context_cap.turn
            context_notice("handoff_requested" if action == "steer" else "cap_interrupt_requested")
            send({"id": rid, "method": "turn/" + action, "params": params})


def wait_turn():
    """Drain both app-server actions and real OS jobs before any turn can end this lane."""
    last_jobs = None
    while True:
        draining = done["turn"] is not None or context_cap.requested
        jobs = native_jobs() if draining else []
        if draining and jobs != last_jobs:
            record(
                "lifecycle",
                {
                    "state": "native_jobs_draining" if jobs else "native_jobs_drained",
                    "pids": jobs,
                    "thread_id": thread,
                    "at": time.time(),
                },
            )
            last_jobs = jobs
        if draining and jobs:
            # The wall cannot turn a safe RESULT.md/context drain into a kill of a native writer.
            signal.alarm(0)
        context_signals(jobs)
        if context_cap.handoff_deadline is not None and time.monotonic() >= context_cap.handoff_deadline:
            if context_cap.active and not jobs:
                # HANDOFF.md is written. Actions with no live process can never be drained by waiting: their
                # end event was lost. Only a live native writer is allowed to hold the lane past the deadline.
                context_notice(
                    "handoff_deadline_ghost_actions",
                    "handoff deadline exceeded; actions with no live process: " + ",".join(sorted(context_cap.active)),
                )
                stop(8, "RolloverFailed: handoff deadline exceeded with no live process for active actions")
            elif jobs:
                if not context_cap.handoff_deadline_reported:
                    context_cap.handoff_deadline_reported = True
                    context_notice(
                        "handoff_deadline_draining",
                        "60s handoff deadline exceeded; advancing jobs/actions retained, no successful successor",
                    )
            elif done["turn"] is None:
                context_notice("rollover_failed", "60s handoff deadline exceeded awaiting turn completion")
                stop(8, "RolloverFailed: handoff deadline exceeded awaiting turn completion")
        if draining and not context_cap.active and not jobs:
            remaining = wall_deadline - time.monotonic()
            if remaining <= 0:
                stop(6, "WallReached after native jobs drained")
            signal.setitimer(signal.ITIMER_REAL, remaining)
        if done["turn"] is not None and not context_cap.active and not jobs:
            remaining = wall_deadline - time.monotonic()
            if remaining <= 0:
                stop(6, "WallReached after native jobs drained")
            signal.setitimer(signal.ITIMER_REAL, remaining)
            return
        descriptors = []
        try:
            for pid in jobs:
                try:
                    descriptors.append(os.pidfd_open(pid))
                except ProcessLookupError:
                    pass
            if jobs and not descriptors:
                continue
            if not buf:
                # The existing event loop also observes file completion while the model reasons.
                ready, _, _ = select.select([sock, *descriptors], [], [], 1 if context_cap.requested else None)
                if sock not in ready:
                    continue
            handle(recv_message())
        finally:
            for descriptor in descriptors:
                os.close(descriptor)


def call(method, params, failure=None, deadline=None):
    global rpc_deadline
    serial[0] += 1
    rid = str(serial[0])
    previous_timeout, previous_deadline = sock.gettimeout(), rpc_deadline
    # A missing rollover RPC response must fail promptly, independently of a long lane wall.
    if failure:
        rpc_deadline = time.monotonic() + 30
        if deadline is not None:
            rpc_deadline = min(rpc_deadline, deadline)
        remaining = rpc_deadline - time.monotonic()
        if remaining <= 0:
            stop(8, "%s: handoff deadline exceeded before %s" % (failure, method))
        sock.settimeout(remaining)
    try:
        send({"id": rid, "method": method, "params": params})
        while True:
            f = recv_message()
            if f.get("id") == rid and "method" not in f:
                record("in", f)
                if f.get("error"):
                    stop(
                        8 if failure else 4,
                        "%s: %s: %s" % (failure or "CodexRPCRefused", method, json.dumps(f["error"])),
                    )
                return f.get("result")
            handle(f)
    except OSError as error:
        if failure:
            stop(8, "%s: %s: %s" % (failure, method, error))
        raise
    finally:
        rpc_deadline = previous_deadline
        sock.settimeout(previous_timeout)


call("initialize", {"clientInfo": {"name": "decomp_lane", "version": "1"}, "capabilities": {"experimentalApi": True}})
send({"method": "initialized"})
usage = call("account/rateLimits/read", {})
limits = (usage.get("rateLimitsByLimitId") or {}).values()
limit = next((v for v in limits if v.get("normalModelSlug") == a.model), usage.get("rateLimits") or {})
record("usage", usage)
# A reached plan limit still runs on credits; only no credits or a reached spend control stops the lane.
credits = limit.get("credits") or {}
if (
    usage.get("ordinaryUsageAllowed") is False
    or limit.get("rateLimitReachedType")
    or any((limit.get(k) or {}).get("usedPercent", 0) >= 100 for k in ("primary", "secondary"))
) and (not (credits.get("hasCredits") or credits.get("unlimited")) or limit.get("spendControlReached")):
    stop(7, "UsageLimitExceeded before turn: " + json.dumps(limit)[:400])


def publish_thread():
    destination = os.path.join(a.dir, "thread.id")
    with open(destination + ".new", "w") as output:
        output.write(thread)
    os.replace(destination + ".new", destination)


def start_thread(resume=None, rollover=False, deadline=None):
    global thread
    params = {"cwd": a.cwd, "model": a.model}
    if resume is not None:
        params["threadId"] = resume
    started = call(
        "thread/resume" if resume is not None else "thread/start",
        params,
        "RolloverFailed" if rollover else None,
        deadline=deadline,
    )
    started = started if isinstance(started, dict) else {}
    candidate = (started.get("thread") or {}).get("id") or started.get("threadId")
    if not candidate or (rollover and candidate == thread):
        stop(
            8 if rollover else 4,
            "%s: invalid new thread id: %s"
            % ("RolloverFailed" if rollover else "ThreadIdMissing", json.dumps(started)[:300]),
        )
    if resume is not None and candidate != resume:
        stop(4, "ThreadIdMismatch: resumed thread differs from explicit requested id")
    if resume is not None and started.get("model") != a.model:
        stop(4, "ThreadModelMismatch: resumed model differs from explicit requested model")
    thread = candidate
    publish_thread()
    call(
        "thread/name/set",
        {"threadId": thread, "name": a.name},
        "RolloverFailed" if rollover else None,
        deadline=deadline,
    )


start_thread(a.resume_thread)
BACKOFF = (30, 60, 120, 240, 480, 600)
resumes = 0


def completion():
    # Only explicit handoff declarations are migrated; ordinary RESULT.md stays the sole contract.
    result = os.path.join(a.dir, "RESULT.md")
    said = open(result).read() if os.path.exists(result) else ""
    if context_cap.requested and handoff_only_result(said):
        archived = os.path.join(a.dir, "HANDOFF-RESULT-%d.txt" % (resumes + 1))
        os.replace(result, archived)
        record("lifecycle", {"state": "handoff_result_preserved", "path": archived})
        said = ""
    return is_done(a.done_when or "result", said)


def lifecycle_text():
    return (
        "\n\nCONDITIONAL CONTEXT POLICY, not an active handoff request: "
        "Continue the original task normally and write RESULT.md for actual work completion. "
        "Only when observed thread/tokenUsage/updated inputTokens reaches 230000, or the runner "
        "sends its active context-handoff steer, finish the current safe operation and write "
        "HANDOFF.md with state paths, metrics, exact next action and open routes, then end the turn. "
        "For that unfinished handoff, never write RESULT.md; preserve/remove any handoff-only RESULT.md. "
        "At 256000 the runner interrupts at a safe action boundary after native writers drain. "
        "A fresh HANDOFF at a drained boundary starts the same runtime60s successor deadline; "
        "rewrites do not reset it and model-summary waiting is interrupted safely. "
        "%s "
        "Steers and STANDING-ORDERS.md carry over into every continuation brief. "
        "Do not compact. These conditional lifecycle rules supersede older instructions to "
        "handoff immediately, write RESULT.md on handoff, or wait for an owner relaunch. "
        "Lane output directory: %s. Completion rule: --done-when %s."
        % (successor_text(), a.dir, a.done_when or "result")
    )


def brief_text():
    # Accepted steers are appended to the brief by steer.py; carry them into every successor.
    return open(a.brief).read() + lifecycle_text()


def rollover(handoff_before):
    global context_cap, resumes, preserve_state
    preserve_state = True
    handoff_path = os.path.join(a.dir, "HANDOFF.md")
    if file_version(handoff_path) in (None, handoff_before):
        context_notice("rollover_failed", "missing fresh HANDOFF.md")
        stop(8, "RolloverFailed: cap reached without a fresh HANDOFF.md in " + a.dir)
    handoff = open(handoff_path).read()
    if not handoff.strip():
        stop(8, "RolloverFailed: HANDOFF.md is empty")
    if time.monotonic() >= wall_deadline:
        stop(6, "WallReached before rollover")
    old_thread, peak = thread, context_cap.peak
    observed_at, eligible_at = context_cap.handoff_first_observed_at, context_cap.handoff_first_eligible_at
    deadline, deadline_at = context_cap.handoff_deadline, context_cap.handoff_deadline_at
    context_notice("rollover_starting")
    # No server/socket/unit changes. Discard only this thread's lifecycle and turn state.
    context_cap = ContextCap()
    final["text"] = None
    done["turn"] = None
    overloaded[0] = False
    context_calls.clear()
    start_thread(rollover=True, deadline=deadline)
    resumes += 1
    entry = {
        "at": time.time(),
        "resume": resumes,
        "old_thread_id": old_thread,
        "new_thread_id": thread,
        "input_tokens_peak": peak,
    }
    entry.update(
        handoff_first_observed_at=observed_at,
        handoff_first_eligible_at=eligible_at,
        handoff_deadline_at=deadline_at,
        handoff_to_new_thread_seconds=time.monotonic() - (deadline - 60) if deadline is not None else None,
        handoff_latest_mtime=file_version(handoff_path)[1] / 1e9,
    )
    with open(os.path.join(a.dir, "ROLLOVERS.jsonl"), "a") as output:
        output.write(json.dumps(entry, sort_keys=True) + "\n")
    with open(os.path.join(a.dir, "STATUS.md"), "a") as output:
        output.write(
            "\nROLLOVER %d %s: %s -> %s; peak input tokens=%d; resume from HANDOFF.md.\n"
            % (resumes, time.strftime("%Y-%m-%dT%H:%M:%S%z"), old_thread, thread, peak)
        )
    record("rollover", entry)
    context_notice("rollover_running")
    return brief_text() + "\n\nHANDOFF.md contents:\n" + handoff + "\n\nRESUME %d: continue from HANDOFF" % resumes


def run_turn(text):
    """Rollover resumes one logical turn; ordinary --continues and the wall never reset."""
    attempt = 0
    while True:
        if time.monotonic() >= wall_deadline:
            stop(6, "WallReached before turn")
        handoff_before = file_version(os.path.join(a.dir, "HANDOFF.md"))
        context_cap.handoff_before = handoff_before
        overloaded[0] = False
        done["turn"] = None
        final["text"] = None
        call(
            "turn/start",
            {"threadId": thread, "input": [{"type": "text", "text": text}]},
            "RolloverFailed" if resumes and attempt == 0 else None,
        )
        wait_turn()
        if context_cap.requested:
            if completion():
                return
            # Owner 2026-10-13: a lane closes at its first context cap; the orchestrator relaunches fresh.
            if os.environ.get("LANE_ROLLOVER") != "1":
                stop(
                    9,
                    "ContextCap: closed at first cap (peak %d); HANDOFF.md %s"
                    % (
                        context_cap.peak,
                        "written"
                        if file_version(os.path.join(a.dir, "HANDOFF.md")) not in (None, handoff_before)
                        else "MISSING",
                    ),
                )
            text = rollover(handoff_before)
            # A rollover does not reset the retry budget either.
            continue
        if not overloaded[0] or final["text"] is not None:
            return
        if attempt == len(BACKOFF):
            stop(6, "ServerOverloaded: %d turns in a row ended at capacity" % (attempt + 1))
        record("lane", {"retry": attempt + 1, "wait_s": BACKOFF[attempt], "why": "serverOverloaded"})
        time.sleep(BACKOFF[attempt])
        attempt += 1
        text = "The previous turn was cut off by a server capacity error. Continue the task from where you stopped."


run_turn(brief_text())
KEEP_GOING = {
    "identical": (
        "You still have wall time and the function is not identical. Keep going: take the region with the most "
        "differing words, fix it change by change with a try after each, log every try, and rewrite RESULT.md when you stop."
    ),
    "result": (
        "You still have wall time and RESULT.md is not written yet. Continue the brief from where you stopped. "
        "Re-read the brief if needed, finish the remaining steps, and write RESULT.md before the wall."
    ),
}[a.done_when or "result"]
for more in range(a.continues or 0):
    if completion():
        break
    if time.monotonic() >= wall_deadline:
        stop(6, "WallReached before continuation")
    record("lane", {"continue": more + 1})
    run_turn(KEEP_GOING + lifecycle_text())
if final["text"] is not None:
    open(os.path.join(a.dir, "final.md"), "w").write(final["text"])
status = done["turn"].get("status")
stop(
    0 if status == "completed" and final["text"] is not None else 5,
    "turn %s, final answer %s" % (status, "written" if final["text"] is not None else "missing"),
)
