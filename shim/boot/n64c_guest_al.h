/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_GUEST_AL_H
#define N64C_GUEST_AL_H
/* Audio imports at the original 32-bit C ABI. Guest record names describe
 * caller-owned storage; native implementations live in n64c_al.c. No SDK code. */
struct ListLink;
struct ALCSeq_s;
struct ALCSeqMarker;
struct ALCMidiHdr;
struct ALEvent;
void alUnlink(struct ListLink *link);
void alLink(void *link, void **previous);
void alClose(int *globals);
int alCSPGetState(void *player);
void alCSPSetChlFXMix(int player, int channel, signed char mix);
void alCSPPlay(int player);
float alCSeqTicksToSec(void **sequence, int ticks, int tempo);
unsigned int alCSeqSecToTicks(void **sequence, float seconds, int tempo);
int alCSeqGetTicks(void *sequence);
void alCSeqGetLoc(struct ALCSeq_s *sequence, struct ALCSeqMarker *marker);
void alCSeqSetLoc(struct ALCSeq_s *sequence, struct ALCSeqMarker *marker);
void alCSeqNewMarker(struct ALCSeq_s *sequence, struct ALCSeqMarker *marker, unsigned int ticks);
void alCSeqNew(struct ALCSeq_s *sequence, struct ALCMidiHdr *data);
void alCSeqNextEvent(struct ALCSeq_s *sequence, struct ALEvent *event);
int __alCSeqNextDelta(struct ALCSeq_s *sequence, int *ticks);
#endif
