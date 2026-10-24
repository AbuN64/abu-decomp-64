# Architecture

| Module | Contract | Responsibility |
| --- | --- | --- |
| Shim | abu-decomp-64 `shim/` (pinned in `cmake/shim.cmake`) | The N64 OS, display-list, audio and controller layer, Fast3D glue and the boot tools. Game-agnostic: it holds no facts of {{title}}. |
| Port facts | `platform/port/` | The manifest of the original source (`manifest.json`), the bindings of its libultra functions to shim functions, the boot trace, accepted lockstep differences, and the ENTRY_POINTS ledger of what still depends on the shim. The shim's boot tools read them through `N64C_PORT_DIR`. |
| Engine | `src/{common,platform,renderer,sound,input,install}` | Engine dirs, no game facts (see `docs/FORMAT.md`). |
| Port code | `src/` | Code written for this port. It reaches N64 names only through `platform/port`; `n64lint` enforces it. |

`port.json` holds this port's project facts (shim pin, original source repository and version, entry); the code reads none of them at run time. The ledger stays per port: phase 2 removes the port's use of the shim row by row, so its rows only fall.
