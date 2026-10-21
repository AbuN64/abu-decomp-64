# Playbooks

Steering text for whoever drives a decompilation and its native port with this
tool: an orchestrator, a lane, or a person. Each playbook names one class of
problem, the order of attack that worked, and the commands that carry it. Read
the one that matches the phase you are in before planning a lane or a fix.

| Playbook | Phase | Use when |
|---|---|---|
| [01-first-boot-speedrun](01-first-boot-speedrun.md) | Port boots to its first original frame | The decomp is mostly matched and the port does not yet draw the game |
| [02-tas-behaviour-loop](02-tas-behaviour-loop.md) | Port runs; the last functions and behaviours | The port draws frames and the remaining failures show up only while playing |

Rules every playbook shares:

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
