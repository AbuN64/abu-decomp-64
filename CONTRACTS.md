# Contracts

The uppercase requirement words MUST, MUST NOT, SHOULD and MAY follow [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

## Roles

- **unbake** is the objective framework with the hard rules (setup, score, submit, land, report).
- **Recipe Maker** reads the ROM and sends objective work through unbake; the language model supplies names, purpose and structure.
- **Cake Taste** is the standalone prodder: recomp, repro, probes, traces, handoff packets. It depends on no decomp tool.
- **abu-decomp-64** is the dev cycle and its seat. It depends on all three and acts on them only through their public CLIs and published files.

Project facts (paths, addresses, titles, repositories, ports, lane names) live in profiles and specs, never in this package.

## Modules

| Module | Boundary |
|---|---|
| `cli` | One entry point. Every command prints one envelope `{ok, result, findings, stages, invocation}`; help is plain text and `mcp` speaks MCP on stdout. Commands register in `COMMANDS` with their own argument definitions. |
| `config` | One explicit JSON seat profile. Unknown or missing top-level keys fail with `file:key`. Each component validates its own section: `tools`, `events`, `stream`, `console`, `lanes`, `supervisor`, `build`. |
| `tools` | Pins and passthrough. Pins are this distribution's own direct requirements, read from its installed metadata. `decomp dependencies` requires each tool's `direct_url.json` at the pinned commit, Recipe Maker on the same unbake commit, and Cake Taste free of unbake and Recipe Maker. `decomp --version` prints decomp's and the pinned tools' versions. `decomp unbake|recipe|taste ARGS` runs the tool's `main(argv)` in an isolated interpreter with ARGS untouched (argparse never sees them); a call that starts with an option or contains `--help`/`-h` (`--version`, `score --help`) is answered in the tool's own text and exit code, not a JSON envelope; project, host and Taste profile come from the `tools` section; `decomp unbake|recipe --project DIR [--host FILE] ...` targets another tree (a lane's own) with the same pinned tool and is refused when that tree's pin file (`tools.pin_file`, default `ci/run`, a wheel URL naming the version) pins another version than the one installed. |
| `events` | Imports the Cake Taste writer (rule below) and requires event schema 2. Section `events`: `{path}`. `decomp event append|tail|validate`; `tail` shows the schema 2 kind of every line. `RUNG` is an unknown kind everywhere (no reader map; old feeds are rewritten once outside the tool). |
| `stream` | Compositor, tile grid, transport, capture. A tile is a video role or a console. The browser page has no controls: the video is always fitted to the window (no scrollbars, no native-size mode) and audio starts as soon as the browser allows, on the first click or key anywhere. `stream.up.cursor = "hidden"` gives the compositor and its clients a transparent cursor theme so only the viewer's own cursor shows. The tile grid is any rectangle of at least two rows and two columns (2x2, 3x2, ...) in the seat `tiles.layout`; a role id repeated down one column is one tall tile, and a video role's `overlay_app_id` window floats exactly over its tile. |
| `console` | The one Textual console: one compact title line, no key footer; rows run oldest to newest, each with a live dim age column (`12s`, `4m`, `2h`), and every tile follows the bottom until the user scrolls up (End or the `↓ latest` control resumes). Panes read files and CLI JSON only: progress.json, `unbake report --next`, event feeds, run status, lane PULSE.json and transcripts. The `matching` pane (`console.panes.matching`: `lanes` glob, `outcomes`, `scores`, `matched`, `active_seconds`, `progress`) shows one block per active lane: the function in the newest score, its best variant's words matched out of target, the latest refusal as `path:line key: message`, the last recorded outcome (marked landed once a LAND event names it); its footer counts the functions landed today from LAND messages that follow `decomp land`'s "Fix N ... stand-ins" convention and the `progress` file's function count and code percentage. |
| `lanes` | Launch, steer, end; the model ladder; the registry with its overlap guard. Ladder values and the registry file are workspace configuration. Section `lanes`: `registry`, `ladder`, `codex`, `socket_dir`, `rules`, optional `active_file` (the NAME=PATH list the workspace hygiene reads, kept current by launch, continue and end). The test runner `ci/run_tests.py` (used by `ci/check`) gives every test a timeout, `ABU_TEST_TIMEOUT` seconds (default 90): a hang dumps every thread's stack and fails that test. Lane supervision (peek, recon, watchers) is the workspace's own hygiene and lives outside this repo. Only `launch`, `continue` and `end` write the registry. `continue --from OLD_DIR --name NEW` launches NEW beside OLD from OLD's brief (old `## Continuation` and rules dropped, one continuation pointing at OLD's HANDOFF.md and STATUS.md, rules appended), inheriting the spec from OLD's registry row or its unit's ExecStart, and refuses while OLD's unit is active (`--wait SECONDS` waits for it to close). `launch` also refuses (`lanes.tool_pin`) when the pin file of the lane's `--cwd` project names a tool version other than the installed one. The runner continues its own lane when it closes on an unfinished RESULT (`result` lanes end only on a `DONE` line in RESULT or RESULT.md) or a context handoff: a transient `relaunch-NEW` unit runs `continue --from OLD --name NEW` (trailing number plus one; `--continues` is the budget of links without progress: a link whose RESULT line names a pushed, committed or landed hex commit id, or whose owned git trees gained a commit since the link started, or whose declared `--export PATH` file (repeatable, absolute, under an `--owns` path, recorded in LAUNCH.json and inherited by `continue`) is newer than the link start with a hash other than at the link start, progressed and keeps the budget, any other link spends one, and a no-progress link at 0 gets a blocked LANE event saying "budget spent"; the event says progressed or spent), so the ladder and overlap guard apply, and every outcome is a LANE event. It needs `--profile`, which `launch` passes. |
| `supervisor` | Runs a target under gdb with retry and capture, the boot gate, RUN-FAIL events. Headless; the console reads its status file. Section `supervisor`: `{runtime_root, roles}`; `decomp supervisor run|status|show-artifact|viewer --role R`. A role's `env` and the stream's own `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR` and `PULSE_SERVER` reach the supervised process, so a seat can run it windowed (no `--headless`, `SDL_VIDEODRIVER=wayland`, `SDL_AUDIODRIVER=pulse`, `PULSE_SINK` the stream sink): its window floats over its video tile and, held by the debugger at a boot stop, keeps its last frame. `run` stops on SIGTERM; SIGHUP restarts the run, SIGUSR1 toggles the retry pause. RUN-FAIL goes to the seat `events` feed. While `run` lives its pid is in `<status_path>.pid`; a console tile whose sources name `supervisor_status` sends SIGHUP on F5 and SIGUSR1 on F7. |
| `build` | Committed-source build and verified releases (`build/SCHEMA.md`). Section `build`: the build tables plus `project_root`, `runtime_root`, `output_root`. `decomp build once [--prove-only]|status`. |
| `cycle` | `decomp cycle`: one blocking inotify loop over the section's source roots and refs (RefSource; a refs table's `pull_seconds` only runs `git pull --ff-only`). Each settled burst runs, each when configured: `build` (a fresh `decomp build ARGS`), `recipe`, `unbake report` with MATCH on the delta; `run_at_start` runs once first; `propose` runs the Recipe proposals on the files the landed commits changed and appends one LAND event with the summary (a `land_refs` commit then runs only this step); `land_refs` otherwise only appends LAND. A failed step is RUN-FAIL, and so is a ref repo whose `git pull --ff-only` fails (diverged, dirty, fetch error), once per reason with ahead/behind counts. |
| `proposals` | Recipe proposals after a landing, used by `land` and `cycle`: each step is `{name, args}` of a Recipe verb that only proposes (`structure`, `style`, `flags`, `look`, `status`, `name next|banners`); `{files}` expands to the landed sources. Nothing is applied (`recipe apply`, `recipe name submit` are refused in the profile), so no rename can invalidate a lane's pending patch; a failing step is a finding, never a failed landing. |
| `probe` | Oracle probe requests. Section `probe`: `{queue}`. `decomp probe request` writes `<queue>/requests/<id>.json` once and appends HANDOFF with `subject.address`; `serve` lists the open ones oldest first with the Taste repro `story` (its PROBE events carry `payload.request` = id); `status` joins requests to PROBE events: open, running, answered, superseded (a later request's `supersedes`). The console `experiments` pane (`{queue, limit}`) shows open requests with age, the last PROBE per owner address with match_state, and host counters. |
| `land` | `decomp land --tree T --base REF --patch LANE=PATH ...` applies lane patches (`--patch LANE=PATH[:MESSAGE]`; `git apply --3way --index`, in order; a failing patch is excluded with git's reason and the tree put back) to a clean proof tree at REF, then runs the gates and stops at the first failure: `unbake setup` when the pinned unbake differs from the tree's last setup, `unbake sync`, the configured check command with `-jN` from the configured admit command, `unbake sync` after check, `unbake sync --check`, then no marker line added in a file no patch touched, no untracked assembly file, no README code percentage below the base. unbake is called only through the pinned tool invoke (`unbake land` lands inbox submissions, a different job). The result lists per-gate pass, per-lane file groups (generated files join the last group) and function counts, a `commits` plan (one `{lane, files, functions, message}` per applied lane, in order; message is the explicit patch message or "Fix N <first marker> stand-ins in <top src dirs> by matching <names> as C", null with a non-blocking `land.commit_message` finding when a lane converted nothing and gave none) with ready `commit_commands` (`git add -- F && git commit -m M -- F`) for the orchestrator to run, and one LAND event is appended. It never commits or pushes. With `land.recipe` (steps of Recipe's proposal verbs, see `proposals`) it ends with a non-gating `recipe` gate that puts the pinned Recipe's names and structure suggestions for the landed source files in the result's `recipe` and the LAND event. Section `land`: `check` plus optional `jobs`, `markers`, `asm_suffix`, `readme`, `timeout_seconds`, `actor`, `recipe`, `recipe_suffixes`. |
| `port` | Generates a port repository skeleton from a port spec (below). |
| `mcp` | The MCP shim (below). |

## Event schema 2

One JSON object per line. Schema 2 is additive over schema 1; every schema 1 line stays valid and is never rewritten.

```json
{"schema": 2, "time": "ISO-8601 with offset", "actor": "lane or tool", "status": "info|started|passed|failed|blocked|decision|handoff",
 "message": "one line of fact", "evidence": "path" | {"path": "...", "sha256": "64 hex"} | null,
 "kind": "RUN-FAIL|PROBE|MATCH|HANDOFF|LAND|LANE|DECISION",
 "subject": {"repo": "", "ref": "", "version": "", "function": "", "address": "0x..."},
 "lane": {"name": "", "kind": "tooling|impl", "model": "", "track": "official|experiment"},
 "payload": {}}
```

- `kind`, `subject`, `lane` and `payload` are optional. Schema 2 kinds are a closed set.
- Schema 1 kinds stay readable: readers show `PORT-FAIL`, `DECOMP` and `COMMIT` as `RUN-FAIL`, `MATCH` and `LAND`. Writers and kind filters accept schema 2 names only.
- The JSON Schema ships with the writer as `cake_taste/logs/event.schema.json`.
- Producers: Cake Taste instruments write PROBE and HANDOFF. decomp writes RUN-FAIL (supervisor), LANE (launch, steer, recycle, end), LAND (landed refs), MATCH (progress delta between two reports) and DECISION. Lanes append HANDOFF and DECISION through the public append command.

## One event writer

Cake Taste owns the only implementation of the event format and its flock append writer. decomp imports it (`cake_taste.logs.events` today) and MUST NOT write event lines itself. A second writer of the same format is a defect.

## Lane overlap guard

`decomp lane launch` refuses a lane when:

- any of its `owns` paths equals, contains or lies inside a live lane's `owns` path;
- it declares no owned paths, or a relative one;
- a lane of the same name is live, or its systemd unit is already active.

This is the one rule that keeps experiment work from interfering with the official decomp. There is no per-track flag; `track` is data the Lanes pane shows. The model ladder is checked at the same point: a lane's model must lie in its kind's range, and the top rung or any rung outside the range needs an owner override reason.

## Lane launch and the done marker

- `decomp lane launch|continue` validate every argument, the ladder, the tool pin and the overlap guard before writing anything. A refusal writes nothing: `continue` creates its new directory and `brief.md` only after every check passed, and removes them if the unit fails to start.

## Port boilerplate

`decomp port new --spec SPEC DEST` writes a port repository into an empty directory: README in the tool README shape, LICENSE, CMake presets with warnings as errors, `src/platform` (the host boundary, the only code that may include OS or device headers), `src/boot` (the original boot chain; the first owner stops with `<subsystem>: original <label> owner <address> has no native implementation`), `tests/boot_gate.txt` with `cmake/boot_gate.cmake` (the executable must print every gate line in order and exit 1 at the stop), `ci/check`, and `port.json` (the spec: the port's project facts). The skeleton fixes the shape; the platform and game internals are the port's own work.

## MCP

`decomp mcp` is a stdio JSON-RPC shim whose code rarely changes.

- `tools/list` derives the tools from a fresh `decomp describe`: `decomp_run(args)` plus one `decomp_<command>(args)` per command group, each carrying the parser's own argument rows.
- `tools/call` runs the current CLI in a fresh interpreter, so edits to decomp take effect on the next call with no reinstall and no restart.
- After each call the shim re-derives the list; if it changed, it sends `notifications/tools/list_changed`.
- Only a change to `mcp.py` itself (the protocol handling) needs the client to restart the server.
