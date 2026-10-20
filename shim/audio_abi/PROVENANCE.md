# Provenance (audio_abi)

Decision (owner relax 17:31): use an existing open-source mixer where one exists, keep our adapter thin.
LUS has none, so the core is the vendored Mupen64Plus-rsp-hle `alist.c` (GPL-2.0-or-later, see SOURCES.md).
No code was read, copied or adapted from sm64-port, sm64ex, libultraship, Ship of Harkinian, Perfect Dark
ports, or any decompiled/leaked Nintendo microcode or SDK source.

| Part | Origin |
|---|---|
| ADPCM decode (`alist_adpcm`), resample (`alist_resample`), mix (`alist_mix`), polef (`alist_polef`), interleave, load/save/move/clear | Vendored, unmodified. Verified by 32 captured tasks (minimum PCM correlation 0.998574). |
| Command decoding and dispatch (`command()` in n64a_abi.c) | Ours. Opcode numbers and operand fields follow the captured Rage Wars lists; each field was cross-checked by the oracle replay (obs.py compares our DMEM/RAM result after every stage with the oracle ucode). The vendored `alist_audio.c` dispatch table was read to confirm layouts. |
| Envelope mixer (`envmix()`) | Ours, written from black-box experiments on the oracle (tools/exp_env.py, exp2-6.py). The vendored envelope mixers (exp, ge, lin, nead) do not match this game's microcode: wrong state layout and ramp law (their output rms was 30x too small). Observed facts: 40-halfword state, per channel 8 lanes of 16.16 volume (high words then low words), target, 16.16 rate, then dry and wet gains; per 8-sample group each lane is multiplied by the rate then clamped to the target (ceiling if rate >= 1.0, floor otherwise); lane k scales sample k with only the high word; outputs use a rounding Q15 multiply and saturating add; init (set-volume driven) lanes ramp linearly to vol*rate across group 0. The init lane step is the ideal value; the oracle deviates from it by under 1 LSB at the end of the group. |
| SETVOL semantic | Observed from the captured init voice (flags 6,4,2,0,8 = left vol, right vol, left target/rate, right target/rate, dry/wet), same as the vendored table. |
| Adapter: PCM sink, ai submit, hle stubs | Ours (trivial). |
| Tests | Ours. Per-command unit tests use hand-built inputs; envelope expectations are numbers recorded from the oracle for the same inputs. |

Residual risks, stated plainly:
- The vendored core's own upstream lineage (an HLE written by the Mupen64 community from the stock microcode's behaviour) is not audited here. Its license is GPL-2.0-or-later; the combined project is distributed under GPL-3.0-or-later, as authorized by the owner. Vendored notices and later-version grants remain unchanged. See the root SOURCES.md and PROVENANCE.md.
- The author of this lane has general prior knowledge of the public RSP audio ABI; it was not consulted from any code during this work.
