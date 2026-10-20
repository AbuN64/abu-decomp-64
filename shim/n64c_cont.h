/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef N64C_CONT_H
#define N64C_CONT_H
#include "n64c_os.h"
#ifdef __cplusplus
extern "C" {
#endif
/* Controller source supplied by the port: a live device or a recorded input script. Fill *pad for the port. */
typedef void (*N64cInputFn)(int port, OSContPad *pad, void *user);
void n64c_cont_set_input(N64cInputFn fn, void *user);
#ifdef __cplusplus
}
#endif
#endif
