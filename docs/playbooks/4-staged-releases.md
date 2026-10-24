# Phase 4: staged releases

## Goal
Ship the port in stages, each one gated by measured coverage of the original code rather than by a feature list. A
stage is never declared from impressions: its numbers come from the full movie set replayed on the build being
released.

## Ladder
The thresholds below are the default; a project's config may set its own.

| Stage | Byte reach | Line | Branch | Other |
|---|---|---|---|---|
| R0 | - | - | - | installs and boots to the title on each platform |
| R1 | 50% | 40% | 30% | no crash or hang in the movie set |
| R2 | 70% | 60% | 45% | functions the reference reaches but the port never: 20 or fewer |
| R3 | 90% | 80% | 65% | none of those; behaviour differences at most 5% of reached functions |
| R4 | phase 3 done | | | |

Line and branch thresholds are lower than byte reach because coverage counters also count defensive code the original
never takes.

## Rules
- Each stage freezes the movie set that proved it; no later build may make one of those movies worse.
- No ROM content is ever packaged: the player supplies their own cartridge dump at first start.
- Enhancements (frame rate, resolution, widescreen, language options) start only after R4, behind options in the game's
  own menu. The 64-bit and frame-rate toggles follow [5-native-port](5-native-port.md).
