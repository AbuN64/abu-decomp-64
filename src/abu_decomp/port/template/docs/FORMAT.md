# Port format 1

This file is the contract of a port repository made by `decomp port new` (abu-decomp-64). Every port carries an identical
copy at `docs/FORMAT.md`; `ci/format-check` enforces the rows marked (checked). Change it only in the template, then
regenerate or copy it into each port in the same wave.

The model is a Quake III style engine around the original game: a small engine (platform, renderer, sound, input,
common, install) and one game module (the original C on the n64 shim). Vanilla stays 1:1 with the cartridge. The
engine has no game facts: every game fact is in `port.json` or `platform/port/`. A second game is a new spec and new
facts, never new engine code.

## Layout (checked: each path exists; nothing else at the top level except dotfiles, LICENSE, README.md, CMake files)

| Path | Owner | Holds |
|---|---|---|
| `port.json` | the port | Project facts: name, title, shim pin, decomp repo/version/pin, boot entry, install facts (game id, title, package, dump table). Format version `"format": 1`. |
| `platform/port/` | the port | The game module's N64 facts, read by the shim boot tools (`N64C_PORT_DIR`): manifest.json, shim-bindings.txt, boot-trace.tsv, ENTRY_POINTS.tsv (only shrinks), sweep rules, baselines, scheduler glue. The only place N64 names may appear outside the shim (n64lint). |
| `src/common/` | engine | Com_*: VFS, cvars, command buffer and console, tick clock, memory, hashing. |
| `src/platform/` | engine | Sys_*: core/time/threads, files, input devices, audio device, video. `win/`, `android/` hold OS backends; posix is the default. |
| `src/renderer/` | engine | R_*: presentation of the game's frames (the shim's Fast3D drives the game's display lists). |
| `src/sound/` | engine | Snd_*: mixing and output of the game's audio. |
| `src/input/` | engine | In_*: devices to usercmds, movie record and replay. |
| `src/install/` | engine | Inst_*: first-run flow (ROM prompt, verify, unpack to the base pack, start). |
| `src/game/` | engine/game boundary | `g_public.h`: the game module API (GAME_API_VERSION, the export and import tables). The module implementation is the decomp C + shim, linked statically in format 1. |
| `src/native/` | the port, optional | Native reimplementations (dev and test only, not shipped). |
| `packaging/<platform>/` | the port | `linux/` (tar.gz, deb, AppImage), `windows/` (zip), `android/` (APK project and build.sh). |
| `tests/unit/` or `tests/native/` | the port | Unit tests (ctest label `unit`). |
| `tests/iface/` | engine | `rw-iface`-style per-interface tests with no ROM (label `iface`). |
| `tests/install/` | engine | Install tests on generated fake ROMs (label `install`). |
| `tools/` | the port | Dev scripts (`tools/win/vm-*.sh`, `tools/android/*.sh`). |
| `cmake/` | the port | Build modules; `shim.cmake` and `decomp.cmake` hold the pins (one `rage_dep` line each). |
| `ci/check` | the port | The local gate: configure, build, ctest, `ci/format-check`. |
| `.github/workflows/ci.yml` | the port | The only workflow; `schedule` (at most twice a day) + `workflow_dispatch`, never push or pull_request (checked). |

## Boundaries (designed now, built only as needed)

1. VFS (`src/common`). Search order, highest first: `<user>/mods/<fs_game>/` (dirs and `.pk3`/zip packs, sorted), then
   `<user>/base/` (the base pack unpacked from the ROM by the installer: the cartridge image `rom.z64` plus the extracted
   asset archives and `MANIFEST`). Every asset read, the cartridge image the shim maps included, resolves through it. With
   `fs_game` empty the game reads exactly the verified cartridge bytes (vanilla 1:1). Format 1 accepts the installer's
   current user tree (`assets/` as the base dir) until INSTALL-GENERIC-1 renames it; the VFS maps `base` to it.
2. Settings (`src/common`). Cvars are the one settings path: command line `+set name value` and `+exec file`, then
   `<user>/config/autoexec.cfg`, then `<user>/config/config.cfg` (written on exit, archived cvars only). Binds are
   commands (`bind KEY "+jump"`). Existing flags (`--rom`, `--user-dir`, `--window`) stay as aliases of cvars.
3. Input as usercmds (`src/input`). One usercmd per game tick per controller: `{tick u32, buttons u16, stick_x s8,
   stick_y s8}` (the N64 controller poll). A TAS movie is a header (game, version, pak, cpak, seed) plus the usercmd
   stream; live play, replay and netcode use the same stream. Determinism is the contract: same header + same usercmds =
   same frames (TAS corpus proves it). Netcode order: lockstep, then rollback, then a dedicated server; none in format 1.
4. Game module (`src/game/g_public.h`). The engine calls `GetGameAPI(&import)` once and gets
   `{api_version, init, run_frame(const usercmd *cmds, unsigned count), shutdown}`; the import table carries the engine
   services the module may call (print, VFS read, cvar get/set, time). In format 1 the module is the static decomp build
   behind the shim (the shim's libultra surface is its syscall layer, bound by `platform/port/shim-bindings.txt`); a mod
   module is the same API as a shared library (`game<arch>.so/.dll`), loaded native first, sandboxed later.

## Install flow (same on Linux, Windows, Android)

Base pack missing or failing verification -> platform ROM prompt (SDL3 file dialog; Android system picker; `--rom PATH`
headless) -> verify against the dump table in `port.json` (byte order normalised; a `.zip` holding exactly one ROM is
accepted) -> unpack into `<user>/base/` atomically -> start. Exit codes: 0 ok, 1 runtime, 2 usage, 3 data missing or
ROM refused, 4 broken install, 78 boot stop. ROM bytes never ship in any package or repository.

## Release stages

R0-R4 are rows of `decomp phase` (seat profile `phase.ladder`), measured on the corpus, never in a port repository.
Packages carry the stage in their version only when the gate held at build time (`0.<stage>.<n>+g<sha>`).
