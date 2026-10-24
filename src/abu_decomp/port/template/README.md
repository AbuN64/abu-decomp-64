# {{name}}

A port of {{title}} on the n64 shim of [abu-decomp-64](https://github.com/AbuN64/abu-decomp-64) (`shim/`, pinned in `cmake/shim.cmake`). The shim is the game-agnostic OS, display-list, audio and controller layer; this repository holds only the game's facts (`platform/port/`) and the port's own code.

> This is a personal tool. It is not officially maintained and may change or break without notice.

## Build

```sh
cmake --preset release && cmake --build --preset release && ctest --preset release
```

The configure fetches the shim and builds `n64compat` 32-bit (`N64C_M32`); it needs CMake 3.28+, Ninja, GCC 14+ with multilib. The first test links the shim; `n64lint` keeps N64 names inside `platform/port`.

## Layout

- `platform/port/`: the port's facts, read by the shim's boot tools through `N64C_PORT_DIR`: `manifest.json`, `shim-bindings.txt`, `boot-trace.tsv`, `lockstep-accept.tsv`, `known-fallback-units.txt`, and the ENTRY_POINTS ledger (`ENTRY_POINTS.tsv`, `OPEN_COUNT`, `VERSION_DEBT`). See the shim's README and `boot/README.md`.
- `src/`: the engine dirs (`common`, `platform`, `renderer`, `sound`, `input`, `install`) and `src/game/g_public.h`, the game module API. N64 names are allowed only in `platform/port`.
- `packaging/`, `tests/{unit,iface,install}`, `tools/`, `ci/`: see `docs/FORMAT.md`, the contract `ci/format-check` enforces.
- `cmake/`: `deps.cmake` (shared dependency sources under `N64C_DEPS_DIR`), `shim.cmake` and `decomp.cmake` (the pins).

## Development

Run `ci/check` (format check, build, tests).

## License

{{license_line}}
