# Port virtual-clock frame cost: measured, not adopted (card sD_h_keZLMW4)

Result: no shim change is committed. Neither candidate cost model passed the per-run proof.

Scoring: per movie, port behaves/differs/missing against the oracle bracket (sd/cmp3.py: newest published-port run vs newest run of each lane label). Installed abu_decomp no longer has tas_coverage.oracle_bracket, so pji-perrun.py does not run as written; the repo source with profile-old.json does.

| lane build | model | same-movie differs, published -> lane |
|---|---|---|
| sd-si2 | SI/Controller Pak transfer latency + VI catch-up | 4356 -> 4337 overall; most runs identical, matrix-16 +4 worse |
| sd-rcp | RSP/RDP frame cost (n64c_rcp.c) | 4340 -> 13665 |
| sd-fin | RCP, tuned | 4340 -> 11109 |
| sd-k4f | RDP_BUS_STALL=4 | 4363 -> 8301 |
| sd-k3f | RDP_BUS_STALL=3 (all 63 runs replayed) | arena movies worse: breakdown-18 170 -> 503, earth-temple-18 168 -> 455, fathom-18 211 -> 596 |
| sd-k6 | RDP_BUS_STALL=6 (3 arena movies) | 714 -> 1495 |

Findings:
- The SI/Pak latency alone moves VI offsets from -77 to -28 on the pak movies (bunker-17, earth-temple-17) but changes function-entry verdicts by at most a few functions; it is not enough for differs 921 -> lower and one run gets worse.
- The RCP cost brings the FT entry count toward the oracle (arena-bunker-17: port 5965 -> 5344, oracle 5289) yet function verdicts get much worse in every arena run: shifting frame time moves the poll phase of every other function. Frame cost cannot be tuned from the entry count alone.
- 30 of 63 runs have no oracle bracket in their run dir, so only about half of the runs are scorable.

Per-movie entry traces (VI offset port-oracle at polls 1000/5000/last; FT entries port before > after): sd-si2 first, then sd-rcp.

## sd-si2
| movie | VI port-oracle @1000 b>a | @5000 b>a | @last b>a | FT entries port b>a (oracle lo..hi) | last poll |
|---|--:|--:|--:|--:|--:|
| a-bunker | -30 > -30 | -31 > -31 | -33 > -33 | 5086 > 5086 (None..None) | 9156 |
| arena-bunker | -30 > -30 | -31 > -31 | -33 > -33 | 5086 > 5086 (None..None) | 9156 |
| boot-missing | -26 > -26 | - > - | -26 > -26 | 2824 > 2824 (2725..None) | 4455 |
| k1 | -30 > -30 | -31 > -31 | -32 > -32 | 4283 > 4283 (3932..None) | 7564 |
| model-preview-8041cad8 | -28 > -28 | - > - | -26 > -26 | 926 > 926 (804..None) | 1214 |
| monkey-options-menu-1-3000 | -26 > -26 | -26 > -26 | -16 > -16 | 6432 > 6432 (6192..None) | 8864 |
| monkey-wl1-start-character-select-0-9-2400 | -26 > -26 | -26 > -26 | -4 > -4 | 5587 > 5587 (None..None) | 7866 |
| monkey-wl1-start-options-menu-0-9-2400 | -26 > -26 | -26 > -26 | -16 > -16 | 5954 > 5954 (5774..None) | 8264 |
| monkey-wl1-start-pak-create-players-0-9-2400 | -26 > -26 | -26 > -26 | -17 > -17 | 6396 > 6396 (6101..None) | 8690 |
| monkey-wl1-start-pak-create-players-2-8-2400 | -77 > -28 | -78 > -29 | -78 > -29 | 4365 > 4365 (4102..None) | 8690 |
| monkey-wl1-start-pak-load-players-0-9-2400 | -26 > -26 | -26 > -26 | -19 > -19 | 6374 > 6374 (6056..None) | 8646 |
| monkey-wl1-start-title-screen-0-9-2400 | -26 > -26 | +1119 > +1119 | +2239 > +2239 | 4711 > 4711 (3392..None) | 6120 |
| monkey-wl1-start-title-screen-2-8-2400 | -77 > -28 | -79 > -30 | -79 > -30 | 3078 > 3078 (2889..None) | 6120 |
| monkey-wl3-multi-character-select-10-p1-2400 | -28 > -28 | -28 > -28 | +1353 > +1353 | 5133 > 5133 (4354..None) | 7866 |
| monkey-wl3-multi-character-select-12-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 4437 > 4437 (4377..None) | 7866 |
| monkey-wl3-multi-pak-create-players-10-p1-24 | -28 > -28 | -28 > -28 | -15 > -15 | 6380 > 6380 (5796..None) | 8690 |
| monkey-wl3-multi-trials-new-game-15-p1-2400 | -26 > -26 | -26 > -26 | +9 > +9 | 6848 > 6848 (4550..None) | 8750 |
| monkey-wl7-arena-bomb-shelter-18-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6271 > 6271 (5312..None) | 11524 |
| monkey-wl7-arena-bottleneck-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6266 > 6266 (5421..None) | 11522 |
| monkey-wl7-arena-boxed-set-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6335 > 6335 (5757..None) | 11656 |
| monkey-wl7-arena-boxed-set-17-p1-2400 | -77 > -28 | -78 > -29 | -88 > -30 | 6015 > 6015 (5383..None) | 11656 |
| monkey-wl7-arena-breakdown-16-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6226 > 6226 (5115..None) | 11440 |
| monkey-wl7-arena-breakdown-18-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6226 > 6226 (5091..None) | 11440 |
| monkey-wl7-arena-bunker-17-p1-2400 | -77 > -28 | -78 > -29 | -89 > -31 | 5965 > 5965 (5289..None) | 11556 |
| monkey-wl7-arena-courtyard-18-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6155 > 6155 (5312..None) | 11306 |
| monkey-wl7-arena-earth-temple-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6148 > 6148 (5602..None) | 11290 |
| monkey-wl7-arena-earth-temple-17-p1-2400 | -77 > -28 | -78 > -29 | -85 > -27 | 5832 > 5832 (5166..None) | 11290 |
| monkey-wl7-arena-earth-temple-18-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6148 > 6148 (5615..None) | 11290 |
| monkey-wl7-arena-fathom-18-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6460 > 6460 (5811..None) | 11906 |
| monkey-wl7-arena-fire-temple-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6348 > 6348 (5650..None) | 11674 |
| monkey-wl7-arena-grim-retreat-17-p1-2400 | -77 > -28 | -78 > -29 | -86 > -28 | 5799 > 5799 (5144..None) | 11224 |
| monkey-wl7-arena-matrix-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6278 > 6278 (5479..None) | 11540 |
| monkey-wl7-arena-matrix-17-p1-2400 | -77 > -28 | -78 > -29 | -88 > -30 | 5957 > 5957 (5196..None) | 11540 |
| monkey-wl7-arena-mystic-16-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6385 > 6385 (5773..None) | 11756 |
| monkey-wl7-arena-mystic-18-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6385 > 6385 (5782..None) | 11756 |
| monkey-wl7-arena-retaliation-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6233 > 6233 (5213..None) | 11456 |
| monkey-wl7-game-setup-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 4872 > 4872 (4346..None) | 8734 |
| monkey-wl7-mode-capture-the-flag-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 5211 > 5211 (4428..None) | 9418 |
| monkey-wl7-mode-team-blood-lust-18-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 5172 > 5172 (4553..None) | 9336 |
| monkey-wl7-trials-character-select-16-p1-240 | -28 > -28 | -28 > -28 | -14 > -14 | 6984 > 6984 (6710..None) | 9050 |
| monkey-wl7-trials-new-game-17-p1-2400 | -77 > -28 | -78 > -29 | -86 > -28 | 4558 > 4558 (4171..None) | 8750 |
| monkey-wl8-arena-bottleneck-19-p1-2400 | -26 > -26 | -26 > -26 | +371 > +371 | 0 > 0 (4683..4687) | 11144 |
| monkey-wl8-arena-bottleneck-21-p1-2400 | -26 > -26 | -26 > -26 | -27 > -27 | 6276 > 6276 (5259..None) | 11522 |
| monkey-wl8-arena-boxed-set-19-p1-2400 | -26 > -26 | -26 > -26 | -1243 > -1243 | 6939 > 6939 (5568..None) | 11656 |
| monkey-wl8-arena-earth-temple-18-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6170 > 6170 (None..None) | 11290 |
| monkey-wl8-arena-fallout-19-p1-2400 | -26 > -26 | -26 > -26 | -989 > -989 | 6699 > 6699 (None..None) | 11374 |
| monkey-wl8-arena-mystic-17-p1-2400 | -77 > -28 | -78 > -29 | -92 > -7 | 7158 > 7158 (None..None) | 11756 |
| monkey-wl8-arena-mystic-21-p1-2400 | -26 > -26 | -26 > -26 | -27 > -27 | 6395 > 6395 (5675..None) | 11756 |
| monkey-wl8-arena-retaliation-21-p1-2400 | -26 > -26 | -26 > -26 | -27 > -27 | 6243 > 6243 (None..None) | 11456 |
| monkey-wl8-mode-capture-the-flag-19-p1-2400 | -26 > -26 | -26 > -26 | -2090 > -2090 | 5480 > 5480 (4947..None) | 9418 |
| monkey-wl8-mode-team-blood-lust-20-p1-2400 | -71 > -22 | -74 > -25 | -85 > -18 | 5815 > 5815 (None..None) | 9336 |
| monkey-wl8-oracle-cheats-menu-19-p1-2400 | -26 > -26 | -26 > -26 | +56 > +56 | 5784 > 5784 (None..None) | 7834 |
| monkey-wl8-player-options-21-p1-2400 | -26 > -26 | -26 > -26 | -19 > -19 | 5865 > 5865 (None..None) | 8734 |
| monkey-wl8-sound-options-19-p1-2400 | -26 > -26 | -26 > -26 | -1810 > -1810 | 5331 > 5331 (None..None) | 8690 |
| monkey-wl8-title-screen-17-p1-2400 | -77 > -28 | -86 > -19 | -97 > -12 | 4334 > 4334 (3927..None) | 6120 |
| monkey-wl8-trials-new-game-20-p1-2400 | -71 > -22 | -74 > -25 | -83 > -16 | 5520 > 5520 (None..None) | 8750 |
| pak-continue-legal | -26 > -26 | - > - | -26 > -26 | 1796 > 1796 (None..None) | 2591 |
| wall2-pads-3-forward | -29 > -29 | -32 > -32 | -32 > -32 | 5118 > 5118 (None..None) | 9244 |
| wall3-menu-controller-pak-menu | -91 > -42 | -107 > -58 | -127 > -78 | 5053 > 5053 (None..None) | 10056 |
| wall3-menu-player-options | -30 > -30 | -31 > -31 | -30 > -30 | 8227 > 8227 (None..None) | 10570 |
| wall3-pause-menu | -30 > -30 | -31 > -31 | -33 > -33 | 6282 > 6282 (None..None) | 11554 |
| wall6-3-pads-blood-lust | -29 > -29 | -32 > -32 | -33 > -33 | 4714 > 4714 (None..None) | 8418 |
| wall6-4-pads-tag-the-monkey | -30 > -30 | -34 > -34 | -40 > -40 | 4802 > 4802 (None..None) | 8596 |

## sd-rcp (K from tuning)
| movie | VI port-oracle @1000 b>a | @5000 b>a | @last b>a | FT entries port b>a (oracle lo..hi) | last poll |
|---|--:|--:|--:|--:|--:|
| a-bunker | -30 > -30 | -31 > -31 | -33 > -33 | 5086 > 4665 (None..None) | 9156 |
| arena-bunker | -30 > -30 | -31 > -31 | -33 > -33 | 5086 > 4665 (None..None) | 9156 |
| boot-missing | -26 > -26 | - > - | -26 > -26 | 2824 > 2744 (None..None) | 4455 |
| k1 | -30 > -30 | -31 > -31 | -32 > -32 | 4283 > 3740 (3932..None) | 7564 |
| model-preview-8041cad8 | -28 > -28 | - > - | -26 > -26 | 926 > 926 (None..None) | 1214 |
| monkey-options-menu-1-3000 | -26 > -26 | -26 > -26 | -16 > -16 | 6432 > 6432 (6192..None) | 8864 |
| monkey-wl1-start-character-select-0-9-2400 | -26 > -26 | -26 > -26 | -4 > -4 | 5587 > 5587 (None..None) | 7866 |
| monkey-wl1-start-options-menu-0-9-2400 | -26 > -26 | -26 > -26 | -16 > -16 | 5954 > 5954 (None..None) | 8264 |
| monkey-wl1-start-pak-create-players-0-9-2400 | -26 > -26 | -26 > -26 | -17 > -17 | 6396 > 6396 (None..None) | 8690 |
| monkey-wl1-start-pak-create-players-2-8-2400 | -77 > -28 | -78 > -29 | -78 > -29 | 4365 > 4136 (None..None) | 8690 |
| monkey-wl1-start-pak-load-players-0-9-2400 | -26 > -26 | -26 > -26 | -19 > -19 | 6374 > 6374 (None..None) | 8646 |
| monkey-wl1-start-title-screen-0-9-2400 | -26 > -26 | +1119 > +1106 | +2239 > +2226 | 4711 > 4698 (3392..None) | 6120 |
| monkey-wl1-start-title-screen-2-8-2400 | -77 > -28 | -79 > -30 | -79 > -30 | 3078 > 2946 (None..None) | 6120 |
| monkey-wl3-multi-character-select-10-p1-2400 | -28 > -28 | -28 > -28 | +1353 > +1353 | 5133 > 5133 (4354..None) | 7866 |
| monkey-wl3-multi-character-select-12-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 4437 > 4437 (4377..None) | 7866 |
| monkey-wl3-multi-pak-create-players-10-p1-24 | -28 > -28 | -28 > -28 | -15 > -15 | 6380 > 6380 (None..None) | 8690 |
| monkey-wl3-multi-trials-new-game-15-p1-2400 | -26 > -26 | -26 > -26 | +9 > -14 | 6848 > 5278 (4550..None) | 8750 |
| monkey-wl7-arena-bomb-shelter-18-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6271 > 4431 (None..None) | 11524 |
| monkey-wl7-arena-bottleneck-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6266 > 4263 (5421..None) | 11522 |
| monkey-wl7-arena-boxed-set-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6335 > 4752 (5757..None) | 11656 |
| monkey-wl7-arena-boxed-set-17-p1-2400 | -77 > -28 | -78 > -29 | -88 > -30 | 6015 > 5269 (None..None) | 11656 |
| monkey-wl7-arena-breakdown-16-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6226 > 4440 (5115..None) | 11440 |
| monkey-wl7-arena-breakdown-18-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6226 > 4549 (5091..None) | 11440 |
| monkey-wl7-arena-bunker-17-p1-2400 | -77 > -28 | -78 > -29 | -89 > -31 | 5965 > 5344 (5289..None) | 11556 |
| monkey-wl7-arena-courtyard-18-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6155 > 4608 (5312..None) | 11306 |
| monkey-wl7-arena-earth-temple-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6148 > 5328 (5602..None) | 11290 |
| monkey-wl7-arena-earth-temple-17-p1-2400 | -77 > -28 | -78 > -29 | -85 > -26 | 5832 > 5276 (5166..None) | 11290 |
| monkey-wl7-arena-earth-temple-18-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6148 > 5251 (5615..None) | 11290 |
| monkey-wl7-arena-fathom-18-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6460 > 4147 (5811..None) | 11906 |
| monkey-wl7-arena-fire-temple-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6348 > 4402 (5650..None) | 11674 |
| monkey-wl7-arena-grim-retreat-17-p1-2400 | -77 > -28 | -78 > -29 | -86 > -27 | 5799 > 5258 (5144..None) | 11224 |
| monkey-wl7-arena-matrix-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6278 > 4752 (5479..None) | 11540 |
| monkey-wl7-arena-matrix-17-p1-2400 | -77 > -28 | -78 > -29 | -88 > -29 | 5957 > 5311 (5196..None) | 11540 |
| monkey-wl7-arena-mystic-16-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6385 > 4320 (5773..None) | 11756 |
| monkey-wl7-arena-mystic-18-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6385 > 4302 (5782..None) | 11756 |
| monkey-wl7-arena-retaliation-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 6233 > 4344 (5213..None) | 11456 |
| monkey-wl7-game-setup-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 4872 > 4007 (4346..None) | 8734 |
| monkey-wl7-mode-capture-the-flag-16-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 5211 > 4009 (4428..None) | 9418 |
| monkey-wl7-mode-team-blood-lust-18-p1-2400 | -28 > -28 | -28 > -28 | -28 > -28 | 5172 > 3967 (4553..None) | 9336 |
| monkey-wl7-trials-character-select-16-p1-240 | -28 > -28 | -28 > -28 | -14 > -14 | 6984 > 6984 (None..None) | 9050 |
| monkey-wl7-trials-new-game-17-p1-2400 | -77 > -28 | -78 > -29 | -86 > -28 | 4558 > 4329 (4171..None) | 8750 |
| monkey-wl8-arena-bottleneck-19-p1-2400 | -26 > -26 | -26 > -26 | - > -34 | 0 > 4554 (4821..None) | 11522 |
| monkey-wl8-arena-bottleneck-21-p1-2400 | -26 > -26 | -26 > -26 | -27 > -27 | 6276 > 4630 (5259..None) | 11522 |
| monkey-wl8-arena-boxed-set-19-p1-2400 | -26 > -26 | -26 > -26 | -1243 > -2286 | 6939 > 4293 (5568..None) | 11656 |
| monkey-wl8-arena-earth-temple-18-p1-2400 | -28 > -28 | -28 > -28 | -29 > -29 | 6170 > 5581 (None..None) | 11290 |
| monkey-wl8-arena-fallout-19-p1-2400 | -26 > -26 | -26 > -26 | -993 > -11 | 6699 > 0 (None..None) | 10996 |
| monkey-wl8-arena-mystic-17-p1-2400 | -77 > -28 | -78 > -29 | -92 > -6 | 7158 > 6364 (None..None) | 11756 |
| monkey-wl8-arena-mystic-21-p1-2400 | -26 > -26 | -26 > -26 | -27 > -27 | 6395 > 4573 (None..None) | 11756 |
| monkey-wl8-arena-retaliation-21-p1-2400 | -26 > -26 | -26 > -26 | -27 > -27 | 6243 > 4916 (None..None) | 11456 |
| monkey-wl8-mode-capture-the-flag-19-p1-2400 | -26 > -26 | -26 > -26 | -2090 > -481 | 5480 > 5194 (None..None) | 9418 |
| monkey-wl8-mode-team-blood-lust-20-p1-2400 | -71 > -22 | -74 > -25 | -85 > -2277 | 5815 > 4361 (None..None) | 9336 |
| monkey-wl8-oracle-cheats-menu-19-p1-2400 | -26 > -26 | -26 > -26 | +56 > +38 | 5784 > 5027 (None..None) | 7834 |
| monkey-wl8-player-options-21-p1-2400 | -26 > -26 | -26 > -26 | -19 > -21 | 5865 > 5574 (None..None) | 8734 |
| monkey-wl8-sound-options-19-p1-2400 | -26 > -26 | -26 > -26 | -1810 > -1439 | 5331 > 0 (None..None) | 8140 |
| monkey-wl8-title-screen-17-p1-2400 | -77 > -28 | -86 > -21 | -97 > -14 | 4334 > 4176 (None..None) | 6120 |
| monkey-wl8-trials-new-game-20-p1-2400 | -71 > -22 | -74 > -25 | -83 > -15 | 5520 > 5373 (None..None) | 8750 |
| pak-continue-legal | -26 > -26 | - > - | -26 > -26 | 1796 > 1796 (None..None) | 2591 |
| wall2-pads-3-forward | -29 > -29 | -32 > -32 | -32 > -32 | 5118 > 5118 (None..None) | 9244 |
| wall3-menu-controller-pak-menu | -91 > -42 | -107 > -58 | -127 > -78 | 5053 > 4678 (None..None) | 10056 |
| wall3-menu-player-options | -30 > -30 | -31 > -31 | -30 > -30 | 8227 > 8227 (None..None) | 10570 |
| wall3-pause-menu | -30 > -30 | -31 > -31 | -33 > -33 | 6282 > 5564 (None..None) | 11554 |
| wall6-3-pads-blood-lust | -29 > -29 | -32 > -32 | -33 > -33 | 4714 > 4169 (None..None) | 8418 |
| wall6-4-pads-tag-the-monkey | -30 > -30 | -34 > -34 | -40 > -40 | 4802 > 4229 (None..None) | 8596 |
