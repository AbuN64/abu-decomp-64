/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_CHARGE_H
#define N64C_CHARGE_H
#include <stdint.h>
/* The original-function entry charge, part of every build (CONTRACTS.md "Work scale"): one tick of guest time per entry
 * of an original game function, by name in the project's charged table (the version's symbols outside the SDK range
 * and not run natively). The compiler's entry call (-finstrument-functions on the original units) lands in
 * __cyg_profile_func_enter (n64c_charge.c) and from there here: a table probe and a counter, no lock, no clock call. */
extern int n64c_work_budget;         /* charged entries left before the clock must move (n64c_sched.c) */
extern char *volatile n64c_runner;   /* the baton holder's n64c_tls_mark */
extern _Thread_local char n64c_tls_mark;
extern const void *const *n64c_charged_fns; /* open-addressed set of the charged functions' addresses */
extern unsigned n64c_charged_mask;
void n64c_work_due(void);            /* the entry that reached the next event: advance the clock to it */
static inline __attribute__((always_inline, no_instrument_function)) void n64c_charge_entry(const void *fn)
{
    if (&n64c_tls_mark != n64c_runner)
        return;
    unsigned s = (((unsigned)(uintptr_t)fn * 2654435761u) >> 7) & n64c_charged_mask;
    for (const void *f; (f = n64c_charged_fns[s]); s = (s + 1) & n64c_charged_mask)
        if (f == fn) {
            if (__atomic_sub_fetch(&n64c_work_budget, 1, __ATOMIC_RELAXED) <= 0)
                n64c_work_due();
            return;
        }
}
/* Resolve the project's charged table and calibration; refuses (boot-missing, exit 7) a build without them.
 * Returns the work scale in 1/2^N64C_WORK_FRAC ns. */
unsigned long long n64c_charge_init(void);
#endif
