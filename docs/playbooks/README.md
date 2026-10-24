# Phase playbooks

How a decompilation and its native port are worked with this tool, in four phases plus one track that runs beside all
of them. Each playbook says what the phase is for, the measurement that ends it, the order of attack and the rules that
hold throughout. They are generic: every game fact (version names, addresses, sizes, constants, figures) lives in the
project's profile and config, and the current numbers come from `decomp phase` and the project's progress block, never
from these pages. For the tool-side steering text of a lane or an orchestrator see [../../Playbooks](../../Playbooks/README.md).

| Playbook | Ends when |
|---|---|
| [1-map-and-foundations](1-map-and-foundations.md) | Every ROM byte is assigned, every function has C, and the engine foundations are exact, clean C |
| [2-port-shell-and-oracle](2-port-shell-and-oracle.md) | A native build and a reference replay the same movie and show the game's first picture |
| [3-coverage-by-play](3-coverage-by-play.md) | One version: every reachable function is executed by play and behaves like the original |
| [4-staged-releases](4-staged-releases.md) | Releases in stages, each gated by measured coverage |
| [track-names-and-real-c](track-names-and-real-c.md) | Parallel track: clean names, file structure, and 100% real C with no cheating |

Throughout every phase:
- **One body serves both goals.** A function lands once, in its real file, as C that builds the original bytes (or,
  under `NON_MATCHING`, behaves like the original) and also compiles for the native port. There is never a port-only
  copy.
- **Reference version first.** One chosen version is the reference; a body is written so every version that holds the
  function builds from it, version differences behind version conditions.
- **Measure on main.** Progress is a number taken on the main branch after landing, not a lane's private tree.
- **Every repeated step becomes a tested tool**, never a lane-local script.
- The project's own check stays green for every version after every change.
