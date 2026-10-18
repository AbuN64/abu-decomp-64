#include "boot/boot.h"

#include "platform/sys_public.h"

const char *Boot_StopMessage(void) {
    return "{{boot_subsystem}}: original {{boot_label}} owner {{boot_owner}} has no native implementation";
}

void Boot_Run(void) {
    Sys_Log("{{boot_subsystem}}: entering {{boot_label}} owner {{boot_owner}}");
    Sys_Error(Boot_StopMessage());
}
