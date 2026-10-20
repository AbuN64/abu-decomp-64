// SPDX-License-Identifier: GPL-3.0-or-later
#include "../fast3d/f3d_context.h"
#include <map>
#include <mutex>
#include <thread>
#include <sys/wait.h>
#include <unistd.h>

static int failures, checks;
#define CHECK(c) do { ++checks; if (!(c)) { ++failures; std::fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #c); } } while (0)
static thread_local SDL_Window* current_window;
static thread_local SDL_GLContext current_context;
static std::map<SDL_GLContext, std::thread::id> owners;
static std::mutex ownership;
static bool reject;
extern "C" SDL_Window* SDLCALL SDL_GL_GetCurrentWindow() { return current_window; }
extern "C" SDL_GLContext SDLCALL SDL_GL_GetCurrentContext() { return current_context; }
extern "C" const char* SDLCALL SDL_GetError() { return "mock context unavailable"; }
extern "C" bool SDLCALL SDL_GL_MakeCurrent(SDL_Window* window, SDL_GLContext context)
{
    std::lock_guard<std::mutex> lock(ownership);
    if (reject || (context && owners.count(context) && owners[context] != std::this_thread::get_id()))
        return false;
    if (current_context) owners.erase(current_context);
    if (context) owners[context] = std::this_thread::get_id();
    current_window = context ? window : nullptr;
    current_context = context;
    return true;
}
static SDL_Window* window = reinterpret_cast<SDL_Window*>(0x1000);
static SDL_GLContext original = reinterpret_cast<SDL_GLContext>(0x2000);
static SDL_GLContext renderer = reinterpret_cast<SDL_GLContext>(0x3000);
static SDL_GLContext nested = reinterpret_cast<SDL_GLContext>(0x4000);

static void early_return()
{
    N64c::F3dContextScope scope(window, renderer);
    CHECK(current_context == renderer);
}
static int failure_status(bool on_leave)
{
    pid_t pid = fork();
    if (!pid) {
        if (!on_leave) reject = true;
        {
            N64c::F3dContextScope scope(window, renderer);
            reject = true;
        }
        _exit(0);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}
int main()
{
    CHECK(SDL_GL_MakeCurrent(window, original));
    // Open creates/binds its new context, but must release it before returning to the guest.
    {
        N64c::F3dContextScope scope;
        CHECK(SDL_GL_MakeCurrent(window, renderer));
    }
    CHECK(current_context == original);
    std::thread task([] {
        CHECK(current_context == nullptr);
        {
            N64c::F3dContextScope scope(window, renderer);
            CHECK(current_context == renderer);
            {
                N64c::F3dContextScope inner(window, nested);
                CHECK(current_context == nested);
            }
            CHECK(current_context == renderer);
        }
        CHECK(current_context == nullptr);
    });
    task.join();
    CHECK(current_context == original);
    early_return();
    CHECK(current_context == original);
    CHECK(failure_status(false) == 78);
    CHECK(failure_status(true) == 78);
    CHECK(current_context == original);
    // Close can be called with the renderer already bound; never restore a retired context.
    CHECK(SDL_GL_MakeCurrent(window, renderer));
    {
        N64c::F3dContextScope scope(window, renderer);
        scope.Retire();
        CHECK(SDL_GL_MakeCurrent(nullptr, nullptr));
    }
    CHECK(current_context == nullptr);
    CHECK(owners.empty());
    std::printf("n64c_f3d_context: %s (%d checks)\n", failures ? "FAIL" : "ok", checks);
    return failures ? 1 : 0;
}
