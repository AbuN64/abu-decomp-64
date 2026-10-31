// SPDX-License-Identifier: GPL-3.0-or-later
// The teardown order is a contract with Mesa's D3D12 driver: the context leaves the thread and is destroyed while its window
// exists, the window goes before the video subsystem, and the subsystem (the driver unload) is quit only when asked.
#include "../fast3d/f3d_teardown.h"
#include <string>
#include <sys/wait.h>
#include <unistd.h>

static int failures, checks;
#define CHECK(c) do { ++checks; if (!(c)) { ++failures; std::fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #c); } } while (0)
static std::string calls;
static bool reject;
extern "C" bool SDLCALL SDL_GL_MakeCurrent(SDL_Window*, SDL_GLContext context)
{
    calls += context ? "bind;" : "release;";
    return !reject;
}
extern "C" const char* SDLCALL SDL_GetError() { return "mock release refused"; }
extern "C" bool SDLCALL SDL_GL_DestroyContext(SDL_GLContext) { calls += "context;"; return true; }
extern "C" void SDLCALL SDL_DestroyWindow(SDL_Window*) { calls += "window;"; }
extern "C" void SDLCALL SDL_QuitSubSystem(SDL_InitFlags flags) { calls += flags == SDL_INIT_VIDEO ? "video;" : "other;"; }

static SDL_Window* window = reinterpret_cast<SDL_Window*>(0x1000);
static SDL_GLContext context = reinterpret_cast<SDL_GLContext>(0x2000);

int main()
{
    N64c::F3dReleaseDisplay(window, context, true);
    CHECK(calls == "release;context;window;video;");
    calls.clear();
    N64c::F3dReleaseDisplay(window, context, false);
    CHECK(calls == "release;context;window;");
    calls.clear();
    N64c::F3dReleaseDisplay(window, nullptr, false);
    CHECK(calls == "window;");
    calls.clear();
    N64c::F3dReleaseDisplay(nullptr, nullptr, false);
    CHECK(calls.empty());
    // A context that will not leave the thread is a failure, not something to destroy around.
    pid_t pid = fork();
    if (!pid) {
        reject = true;
        N64c::F3dReleaseDisplay(window, context, true);
        _exit(0);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 78);
    std::printf("n64c_f3d_teardown: %s (%d checks)\n", failures ? "FAIL" : "ok", checks);
    return failures ? 1 : 0;
}
