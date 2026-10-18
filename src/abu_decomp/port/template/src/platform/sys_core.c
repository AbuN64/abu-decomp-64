#include "platform/sys_public.h"

#include <stdio.h>
#include <stdlib.h>

static bool sys_running;

bool Sys_Init(const Sys_Options *options) {
    if (!options->headless) {
        Sys_Log("Sys_Init: no windowed platform yet; pass --headless");
        return false;
    }
    sys_running = true;
    Sys_Log("Sys_Init: headless platform ready");
    return true;
}

void Sys_Shutdown(void) {
    sys_running = false;
}

void Sys_Log(const char *message) {
    fprintf(stdout, "%s\n", message);
    fflush(stdout);
}

void Sys_Error(const char *message) {
    fprintf(stderr, "ERROR: %s\n", message);
    fflush(stderr);
    if (sys_running)
        Sys_Shutdown();
    exit(1);
}
