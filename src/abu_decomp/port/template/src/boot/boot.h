#ifndef PORT_BOOT_H
#define PORT_BOOT_H

/* Run the boot chain in original order. Each step is native or stops with Sys_Error. */
[[noreturn]] void Boot_Run(void);
/* The message the first unimplemented owner stops with; tests compare against it. */
const char *Boot_StopMessage(void);

#endif
