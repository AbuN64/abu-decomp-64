# Phase playbooks

How a decompilation and its native port are worked with this tool, in five phases plus one track that runs beside all
of them. Each playbook says what the phase is for, the measurement that ends it, the order of attack and the rules that
hold throughout. They are generic: every game fact (version names, addresses, sizes, constants, figures) lives in the
project's profile and config, and the current numbers come from `decomp phase` and the project's progress block, never
from these pages.

| Playbook | Ends when |
|---|---|
| [1-map-and-foundations](1-map-and-foundations.md) | Every ROM byte is assigned, every function has C, and the engine foundations are exact, clean C |
| [2-port-shell-and-oracle](2-port-shell-and-oracle.md) | A native build and a reference replay the same movie and show the game's first picture |
| [3-coverage-by-play](3-coverage-by-play.md) | One version: every reachable function is executed by play and behaves like the original |
| [4-staged-releases](4-staged-releases.md) | Releases in stages, each gated by measured coverage |
| [5-ship-of-theseus](5-ship-of-theseus.md) | The port is replaced plank by plank with our own modern code (no console hacks, native memory, 64-bit, tick rate vs frame rate), each plank proven against the frozen R4 reference; the decompilation stays 1:1 |
| [track-names-and-real-c](track-names-and-real-c.md) | Parallel track: clean names, file structure, and 100% real C with no cheating |

## Tool-side playbooks
Steering text for whoever drives a decompilation and its native port with this tool (an orchestrator, a lane or a person):
one class of problem, the order of attack that worked, and the commands that carry it. Read the one that matches the
phase you are in before planning a lane or a fix.

| Playbook | Phase | Use when |
|---|---|---|
| [01-first-boot-speedrun](01-first-boot-speedrun.md) | Port boots to its first original frame | The decomp is mostly matched and the port does not yet draw the game |
| [02-tas-behaviour-loop](02-tas-behaviour-loop.md) | Port runs; the last functions and behaviours | The port draws frames and the remaining failures show up only while playing |
| [03-play-loop-agent](03-play-loop-agent.md) | Same, run by an agent beside a player | The standing prompt for the agent that reacts to what the player sees |

Rules every tool-side playbook shares:

- Work by **class**, never by stop. A detector runs over every unit and every
  traced function at once; a single crash is one sample of a class.
- **One body serves both goals.** A function lands as C that behaves the same
  as the original, in its real file, and is matched in place later. There is
  never a port-only copy.
- **Measure on main.** Progress is a number taken on the main branch after
  landing, not a lane's private tree or a stop count.
- **Every repeated step becomes a tested tool** here or in the port, never a
  lane-local script.
- Project facts (paths, addresses, titles, ROM names) live in the seat profile
  and the port spec, never in a playbook.

The four phases and the naming/real-C track are in [../docs/playbooks](../docs/playbooks/README.md).

Throughout every phase:
- **One body serves both goals.** A function lands once, in its real file, as C that builds the original bytes (or,
  under `NON_MATCHING`, behaves like the original) and also compiles for the native port. There is never a port-only
  copy.
- **Reference version first.** One chosen version is the reference; a body is written so every version that holds the
  function builds from it, version differences behind version conditions.
- **Measure on main.** Progress is a number taken on the main branch after landing, not a lane's private tree.
- **Every repeated step becomes a tested tool**, never a lane-local script.
- The project's own check stays green for every version after every change.
