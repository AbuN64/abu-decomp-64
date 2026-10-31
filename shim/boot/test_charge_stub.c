/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The scheduler's half of the entry charge (n64c_charge.h), for fixtures that link trace.c without the shim: no thread
 * holds a baton here, so no entry is ever charged. */
#include "../n64c_charge.h"
int n64c_work_budget = 1 << 30;
char *volatile n64c_runner;
_Thread_local char n64c_tls_mark;
const void *const *n64c_charged_fns;
unsigned n64c_charged_mask;
void n64c_work_due(void) {}
