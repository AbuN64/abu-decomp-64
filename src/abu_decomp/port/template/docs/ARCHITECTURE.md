# Architecture

| Module | Contract | Responsibility |
| --- | --- | --- |
| Platform | `src/platform/sys_public.h` | The host boundary: lifecycle, logging, explicit stops. Only platform code includes OS or device headers. |
| Boot | `src/boot/boot.h` | The original boot chain in order. Each owner is native or stops with `has no native implementation`. |
| Application | `src/main.c` | Options, platform start, then the boot chain. |

`tests/boot_gate.txt` lists the boot lines the executable must print, in order, ending at the current stop. When an owner becomes native, its stop line gives way to its real output and the next owner's stop. `port.json` holds this port's project facts; the code reads none of them at run time.
