/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Run-on boot diagnostics. The walk build links decomp/runon.c, which defines these hooks. With N64C_RUN_ON=1 in
 * the environment, a failure that would end the boot is recorded as one "boot-diag:" line (in boot order) and the
 * boot goes on past it wherever the failing site has a defined way on. Without the hooks (the shipped build), or
 * with run-on off, every site stops exactly as before: run-on is a diagnostic, never an acceptance path. */
#ifndef N64C_DIAG_H
#define N64C_DIAG_H
#include "n64c_os.h"
/* A named stop (the text after "boot-missing: "). can_continue: the site has a way on (skip the task, return 0,
 * leave the bytes raw). Returns 1 when the boot goes on; 0 means the site stops as before (when run-on is active it
 * has then also printed the run's failure list). */
int n64c_diag_stop(const char *subject,int can_continue) __attribute__((weak));
/* A call into original code the boot has not linked (a generated stop function). Returns 1 when the call returns 0
 * to its caller and the boot goes on. */
int n64c_diag_missing(const char *symbol) __attribute__((weak));
/* A CPU fault (SIGSEGV, SIGBUS, SIGFPE, SIGILL) no other handler owns. info is a siginfo_t, context a ucontext_t.
 * Returns 1 when the context was rewritten so that the boot resumes (the faulting function returns 0). */
int n64c_diag_fault(int signal_number,void *info,void *context) __attribute__((weak));
/* A call through a code pointer to a guest address: the context is at the faulting fetch, so the call returns 0. */
int n64c_diag_code_pointer(u32 address,void *context) __attribute__((weak));
/* A guest DMA or unpack whose bytes no byte-order row covers: rom/bytes is the uncovered part, ram where it went. */
void n64c_diag_layout(const char *what,u32 rom,u32 bytes,const void *ram) __attribute__((weak));
/* The named boot stop (decomp/missing.c). Linked in the boot driver only. */
long long n64c_boot_missing(const char *symbol) __attribute__((weak));
#endif
