# Sources (audio_abi)

## Code taken in unmodified (vendor/m64p-rsp-hle)
- Project: Mupen64Plus-rsp-hle, https://github.com/mupen64plus/mupen64plus-rsp-hle
- Commit: 8a7a472a7172eb2c8725b305eae26818ed7b51a2 (2026-06-23). Recorded in vendor/m64p-rsp-hle/UPSTREAM.
- License: GNU General Public License v2 or later. Every vendored file carries the "version 2 ... or (at your option) any later version" header; the author list is in vendor/m64p-rsp-hle/LICENSES.
- Files copied byte-for-byte: alist.c alist.h alist_audio.c alist_naudio.c alist_nead.c arithmetics.h audio.c audio.h common.h hle_external.h hle_internal.h memory.c memory.h ucodes.h LICENSES.
- Used by us: the alist_* primitives (ADPCM decode, resample, mix, polef, interleave, load, save, move, clear). Its own command tables (alist_audio.c etc.) are linked but not used; mp3 is stubbed.
- Upstream says its HLE was written from the stock microcode's behaviour (Mupen64 / zilmar lineage). We did not audit that further; see PROVENANCE.md for the residual risk.

## Why not libultraship (checked first, order 1 of the brief)
- The pinned libultraship c6ecfdb (2.0.0.0) has no audio command interpreter: no file in its tree matches aEnvMixer, aSetBuffer, aLoadADPCM or aResample. Its audio part is a PCM player only.

## Public documentation consulted (background only, no code or tables taken)
- https://n64brew.dev/wiki/Audio_Interface : AI is a DMA output unit; decompression and mixing happen on CPU or RSP. It has no command-set pages (n64brew has no RSP audio ABI page at this time).
- https://hackmd.io/@Thar0/rypABUFP9 : VADPCM sample/codebook container description (codebook = order * npredictors * 8 halfwords, loop state). Only used to understand the codebook shape.
- https://ultra64.ca/files/documentation/online-manuals/man/n64man/al/alAudioFrame.html : "commands are 8 bytes each", the CPU builds the list and the RSP interprets it. Read as a search-result snippet only.

## Black-box observation (the oracle)
- The oracle is the cake-taste n64recomp host at $STATE/oracle (test instrument; never shipped, nothing of it is copied). Its output was captured with tools/cap_audio.py (gdb on a private instance) and replayed with tools/oracle_rsp.cpp (the oracle's recompiled audio ucode on a captured RAM image).
- Everything in n64a_abi.c's envelope mixer comes from these observations (see PROVENANCE.md).
