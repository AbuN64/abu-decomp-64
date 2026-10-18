#ifndef PORT_SYS_PUBLIC_H
#define PORT_SYS_PUBLIC_H

#include <stdbool.h>

/* The platform boundary. Game and boot code reach the host only through these calls. */
typedef struct Sys_Options {
    bool headless;
} Sys_Options;

bool Sys_Init(const Sys_Options *options);
void Sys_Shutdown(void);
void Sys_Log(const char *message);
/* Report an unrecoverable stop and exit 1. */
[[noreturn]] void Sys_Error(const char *message);

#endif
