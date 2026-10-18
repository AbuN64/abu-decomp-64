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
| `tools` | Pins and passthrough. Pins are this distribution's own direct requirements, read from its installed metadata. `decomp dependencies` requires each tool's `direct_url.json` at the pinned commit, Recipe Maker on the same unbake commit, and Cake Taste free of unbake and Recipe Maker. `decomp unbake|recipe|taste ...` runs the tool's `main(argv)` in an isolated interpreter; project, host and Taste profile come only from the `tools` section. |
| `events` | Imports the Cake Taste writer (rule below) and requires event schema 2. Section `events`: `{path}`. `decomp event append|tail|validate`; `tail` shows the schema 2 kind of every line. `RUNG` is an unknown kind everywhere (no reader map; old feeds are rewritten once outside the tool). |
| `stream` | Compositor, tile grid, transport, capture. A tile is a video role or a console. |
| `console` | The one Textual console. Panes read files and CLI JSON only: progress.json, `unbake report --next`, event feeds, run status, lane PULSE.json and transcripts. |
| `lanes` | Launch, steer, peek, end; the model ladder; the registry with its overlap guard. Ladder values and the registry file are workspace configuration. Section `lanes`: `registry`, `ladder`, `codex`, `socket_dir`, `rules`, optional `active_file` (the lanepeek NAME=PATH list, kept current by launch, continue and end). `peek` reads that file when set, as lanepeek.py did: NAME=PATH lanes, `job NAME=[HOST:]LOG` lines (an open unbake stage past 5x its last run, clock in `.peek-stages.json` beside the file) and `rig HOST REGEX` lines (matching commands alive 15+ minutes under 5% CPU); `--name` selects NAME=PATH lines and skips the checks; without the file the live registry rows are the list. Only `launch`, `continue` and `end` write the registry. `continue --from OLD_DIR --name NEW` launches NEW beside OLD from OLD's brief (old `## Continuation` and rules dropped, one continuation pointing at OLD's HANDOFF.md and STATUS.md, rules appended), inheriting the spec from OLD's registry row or its unit's ExecStart, and refuses while OLD's unit is active. |
| `supervisor` | Runs a target under gdb with retry and capture, the boot gate, RUN-FAIL events. Headless; the console reads its status file. Section `supervisor`: `{runtime_root, roles}`; `decomp supervisor run|status|show-artifact|viewer --role R`. `run` stops on SIGTERM; SIGHUP restarts the run, SIGUSR1 toggles the retry pause. RUN-FAIL goes to the seat `events` feed. While `run` lives its pid is in `<status_path>.pid`; a console tile whose sources name `supervisor_status` sends SIGHUP on F5 and SIGUSR1 on F7. |
| `build` | Committed-source build and verified releases (`build/SCHEMA.md`). Section `build`: the build tables plus `project_root`, `runtime_root`, `output_root`. `decomp build once [--prove-only]|status`. |
| `cycle` | `decomp cycle`: one blocking inotify loop over the section's source roots and refs (RefSource; a refs table's `pull_seconds` only runs `git pull --ff-only`). Each settled burst runs, each when configured: `build` (a fresh `decomp build ARGS`), `recipe`, `unbake report` with MATCH on the delta; `run_at_start` runs once first; `land_refs` only appends LAND. A failed step is RUN-FAIL. |
| `probe` | Oracle probe requests. Section `probe`: `{queue}`. `decomp probe request` writes `<queue>/requests/<id>.json` once and appends HANDOFF with `subject.address`; `serve` lists the open ones oldest first with the Taste repro `story` (its PROBE events carry `payload.request` = id); `status` joins requests to PROBE events: open, running, answered, superseded (a later request's `supersedes`). The console `experiments` pane (`{queue, limit}`) shows open requests with age, the last PROBE per owner address with match_state, and host counters. |
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

## Port boilerplate

`decomp port new --spec SPEC DEST` writes a port repository into an empty directory: README in the tool README shape, LICENSE, CMake presets with warnings as errors, `src/platform` (the host boundary, the only code that may include OS or device headers), `src/boot` (the original boot chain; the first owner stops with `<subsystem>: original <label> owner <address> has no native implementation`), `tests/boot_gate.txt` with `cmake/boot_gate.cmake` (the executable must print every gate line in order and exit 1 at the stop), `ci/check`, and `port.json` (the spec: the port's project facts). The skeleton fixes the shape; the platform and game internals are the port's own work.

## MCP

`decomp mcp` is a stdio JSON-RPC shim whose code rarely changes.

- `tools/list` derives the tools from a fresh `decomp describe`: `decomp_run(args)` plus one `decomp_<command>(args)` per command group, each carrying the parser's own argument rows.
- `tools/call` runs the current CLI in a fresh interpreter, so edits to decomp take effect on the next call with no reinstall and no restart.
- After each call the shim re-derives the list; if it changed, it sends `notifications/tools/list_changed`.
- Only a change to `mcp.py` itself (the protocol handling) needs the client to restart the server.
