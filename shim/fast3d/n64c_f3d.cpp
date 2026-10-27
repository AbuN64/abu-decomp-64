// SPDX-License-Identifier: GPL-3.0-or-later
// n64c_f3d: wiring of the pinned Fast3D interpreter for the port (see n64c_f3d.h). The interpreter
// takes its collaborators by injection (Interpreter::Init): the port's window, LUS's OpenGL backend,
// a console-variable store and a resource manager that holds only Fast3D's shader archive. Colour
// images become Fast3D framebuffers through n64c_dl's colour-image hook (OTR_G_SETFB after each
// G_SETCIMG); the root list runs inside [G_DL root][OTR_G_RESETFB][G_ENDDL] so no framebuffer stays bound.
#include "n64c_f3d.h"
#include "f3d_window.h"
#include "n64c_controls.h"
#include "f3d_surface.h"
#include "fast/interpreter.h"
#include "fast/backends/gfx_opengl.h"
#include "fast/debug/GfxDebugger.h"
#include "ship/config/Config.h"
#include "ship/config/ConsoleVariable.h"
#include "ship/resource/ResourceManager.h"
#include "ship/thread/ThreadPool.h"
#ifndef N64C_F3D_NO_EGL
#include <EGL/egl.h>
#include <EGL/eglext.h>
#endif
#include <SDL3/SDL_filesystem.h>
#include <SDL3/SDL_hints.h>
#include <SDL3/SDL_opengl.h>
#include <cstdio>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <map>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

namespace Fast {
void GfxSetInstance(std::shared_ptr<Interpreter> gfx);
}

// Frame-rate unlock (see n64c_f3d.h): what a graphics task asked for, kept so the same lists can be drawn again at a
// moment between two game frames. A group is the tasks of one retrace.
struct MtxRec {
    const int32_t* addr; // the matrix in game memory: Fast3D finds a replacement by this pointer
    MtxF value;
    uint8_t params;
};
struct TaskRec {
    uint32_t dl, other_mode_h, other_mode_h_known, scissor_lry; // the list and the RDP state its translation started from
    uint32_t commands, lists;
    size_t mtx_begin, mtx_end;
};
struct Group {
    std::vector<TaskRec> tasks;
    std::vector<MtxRec> mtx;
    uint32_t surface = 0; // the colour image the frame's last task drew to
};
struct N64cF3d {
    N64cF3dConfig config;
    std::shared_ptr<Ship::Config> settings;
    std::shared_ptr<Ship::ConsoleVariable> cvars;
    std::shared_ptr<Ship::ResourceManager> resources;
    std::shared_ptr<Fast::Interpreter> interpreter;
    N64c::F3dWindow* window = nullptr;            // the interpreter keeps raw pointers to both;
    Fast::GfxRenderingAPIOGL* renderer = nullptr; // they are freed after it in n64c_f3d_close
    N64cDlArena arena = {};
    std::map<uint32_t, int> surfaces;           // colour image physical address -> Fast3D framebuffer
    uint32_t surface = 0;                       // latest colour image bound by the latest run
    uintptr_t wrapper[6] = {};
    std::string gl_renderer;
    bool unlock = false, in_sub = false, have_cur = false, interp = false;
    std::vector<uint8_t> snapshot;              // game memory as the latest task saw it: the interpolated draws read this
    N64cGfxMem snapshot_mem = {};               // (the game goes on and rewrites its lists and data within the same retrace)
    // Fast3D's RDP and RSP state keeps colours, tiles and modes from one list to the next, and a game's list may rely
    // on what the frame before left. An interpolated draw starts from the state its real task started from and puts
    // back the state that task ended in, so the next real task starts exactly as it would without any extra draw.
    Fast::RDP rdp_start, rdp_end;
    Fast::RSP rsp_start, rsp_end;
    std::vector<const int32_t*> sub_seen;
    Group pending, cur, prev;
    std::vector<MtxRec>* capture = nullptr;
    N64cDlArena sub_arena = {};                 // the interpolated draws translate with their own arena and
    std::map<uint32_t, int> sub_surfaces;       // their own framebuffers: the game's colour images stay exactly as it drew them
    uint32_t sub_surface = 0;
    std::string overlay;                        // the options screen's text, drawn over every presented picture
    int overlay_selected = -1, overlay_scale = 3;
    std::string window_shot;                    // a file the next presented window picture (overlay included) is written to
    // The game memory each texture cache entry was decoded from, as it was then (see check_textures).
    std::unordered_map<Fast::TextureCacheKey, std::vector<uint8_t>, Fast::TextureCacheKey::Hasher> decoded;
};
// n64c_dl hook: the framebuffer of this colour image, created on first use at the game's size (its
// scissor's height; the configured one until the game has set a scissor).
static int color_image(void* user, uint32_t phys, uint32_t width, uint32_t height, uintptr_t extra[2])
{
    auto* f3d = static_cast<N64cF3d*>(user);
    const int fb = N64c::ColorImageSurface(*f3d->interpreter, *f3d->renderer,
                                          f3d->in_sub ? f3d->sub_surfaces : f3d->surfaces,
                                          phys, width, height ? height : f3d->config.fb_h);
    (f3d->in_sub ? f3d->sub_surface : f3d->surface) = phys;
    extra[0] = (uintptr_t)(uint8_t)Fast::OTR_G_SETFB << 24;
    extra[1] = (uintptr_t)fb;
    return 1;
}

// The 16.16 fixed-point Mtx as Fast3D reads it (interpreter.cpp GfxSpMatrix).
static MtxF decode_matrix(const int32_t* addr)
{
    MtxF m;
    for (int i = 0; i < 4; i++) {
        for (int j = 0; j < 4; j += 2) {
            const int32_t int_part = addr[i * 2 + j / 2];
            const uint32_t frac_part = (uint32_t)addr[8 + i * 2 + j / 2];
            m.mf[i][j] = (int32_t)(((uint32_t)int_part & 0xffff0000u) | (frac_part >> 16)) / 65536.0f;
            m.mf[i][j + 1] = (int32_t)((uint32_t)int_part << 16 | (frac_part & 0xffffu)) / 65536.0f;
        }
    }
    return m;
}
static void matrix_seen(void* user, const uintptr_t cmd[2])
{
    auto* f3d = static_cast<N64cF3d*>(user);
    if (f3d->in_sub)
        f3d->sub_seen.push_back((const int32_t*)cmd[1]);
    else if (f3d->capture)
        f3d->capture->push_back({ (const int32_t*)cmd[1], decode_matrix((const int32_t*)cmd[1]), (uint8_t)cmd[0] });
}
// SDL's offscreen driver takes the first EGL device that initialises; pick Mesa's software device
// instead so results do not depend on the host GPU.
static void prefer_software_device()
{
#ifdef N64C_F3D_NO_EGL
    return; // Windows: SDL's windows driver creates the context through WGL (Mesa's opengl32.dll on a host without a GPU)
#else
    auto query = (PFNEGLQUERYDEVICESEXTPROC)eglGetProcAddress("eglQueryDevicesEXT");
    auto query_string = (PFNEGLQUERYDEVICESTRINGEXTPROC)eglGetProcAddress("eglQueryDeviceStringEXT");
    EGLDeviceEXT devices[16];
    EGLint count = 0;
    if (!query || !query_string || !query(16, devices, &count))
        return;
    for (EGLint i = 0; i < count; i++) {
        const char* extensions = query_string(devices[i], EGL_EXTENSIONS);
        if (extensions && std::strstr(extensions, "EGL_MESA_device_software")) {
            SDL_SetHint("SDL_HINT_EGL_DEVICE", std::to_string(i).c_str());
            return;
        }
    }
#endif
}

extern "C" int n64c_fp_traps_mask(void);
extern "C" void n64c_fp_traps_restore(int enabled);
namespace {
// Host code (this renderer, the graphics driver) runs with the guest's floating point traps masked (n64c_system.h).
struct FpMasked {
    int enabled = n64c_fp_traps_mask();
    ~FpMasked() { n64c_fp_traps_restore(enabled); }
};
} // namespace
extern "C" N64cF3d* n64c_f3d_open(const N64cF3dConfig* config)
{
    FpMasked fp_masked;
    if (!config || !config->window_w || !config->window_h || !config->fb_w || !config->fb_h)
        return nullptr;
    N64c::F3dContextScope context; // Init creates a context; release it before a guest task runs.
    auto f3d = std::make_unique<N64cF3d>();
    f3d->config = *config;
    std::string shaders = config->shaders ? config->shaders : "", settings = config->settings ? config->settings : "";
    try {
        // The program folder on every platform (SDL resolves the running executable).
        const char* base = SDL_GetBasePath();
        const std::filesystem::path exe = base ? std::filesystem::path(base) : std::filesystem::current_path();
        if (shaders.empty())
            shaders = (exe / "fast3d-shaders.o2r").string();
        if (settings.empty())
            settings = (exe / "fast3d-settings.json").string();
        f3d->settings = std::make_shared<Ship::Config>(settings);
        f3d->cvars = std::make_shared<Ship::ConsoleVariable>(f3d->settings);
        // The resource manager loads through a thread pool even for synchronous loads.
        f3d->resources = std::make_shared<Ship::ResourceManager>(std::make_shared<Ship::ThreadPool>(1));
        f3d->resources->Init(nlohmann::json{{"archivePaths", std::vector<std::string>{shaders}}});
    } catch (const std::exception& e) {
        std::fprintf(stderr, "n64c_f3d: %s: %s\n", shaders.c_str(), e.what());
        return nullptr;
    }
    if (config->offscreen)
        SDL_SetHint(SDL_HINT_VIDEO_DRIVER, "offscreen");
    if (config->software)
        prefer_software_device();
    f3d->interpreter = std::make_shared<Fast::Interpreter>();
    Fast::GfxSetInstance(f3d->interpreter);
    f3d->interpreter->SetGfxDebugger(std::make_shared<Fast::GfxDebugger>());
    f3d->window = new N64c::F3dWindow();
    f3d->renderer = new Fast::GfxRenderingAPIOGL(f3d->cvars, f3d->resources);
    f3d->interpreter->Init(f3d->window, f3d->renderer, "Rage Wars", false, config->window_w, config->window_h, 0, 0,
                           f3d->cvars, f3d->resources);
    // The VI scales the game's colour image onto the screen: map that size onto the window.
    f3d->interpreter->mNativeDimensions.width = config->fb_w;
    f3d->interpreter->mNativeDimensions.height = config->fb_h;
    const char* name = (const char*)glGetString(GL_RENDERER);
    f3d->gl_renderer = name ? name : "";
    return f3d.release();
}

// One translation and Fast3D pass of the list at dl into the arena's colour images (the game's, or with in_sub the
// interpolated draw's own). The caller has reset and primed the arena.
struct SubPlan {
    const Group *prev, *cur;
    float alpha;
};
static MtxF lerp_matrix(const MtxF& a, const MtxF& b, float t);
static void check_textures(N64cF3d* f3d, const N64cGfxMem* mem, bool after);
static int render(N64cF3d* f3d, const N64cGfxMem* mem, uint32_t dl, N64cDlArena* arena, N64cDlStats* stats,
                  const SubPlan* plan)
{
    const N64cDlHooks hooks = { color_image, f3d, matrix_seen };
    f3d->sub_seen.clear();
    const uintptr_t* root = n64c_dl_translate(mem, dl, arena, stats, &hooks);
    if (!root)
        return -1;
    // An interpolated draw moves each matrix of this translation (found by its place in execution order) between where
    // the frame before had it and where this frame does.
    std::unordered_map<Mtx*, MtxF> replacements;
    if (plan) {
        if (f3d->sub_seen.size() != plan->cur->mtx.size())
            return -2;
        replacements.reserve(f3d->sub_seen.size());
        for (size_t i = 0; i < f3d->sub_seen.size(); i++)
            replacements.emplace((Mtx*)f3d->sub_seen[i],
                                 lerp_matrix(plan->prev->mtx[i].value, plan->cur->mtx[i].value, plan->alpha));
    }
    f3d->wrapper[0] = (uintptr_t)0xDE << 24; // G_DL (push)
    f3d->wrapper[1] = (uintptr_t)root;
    f3d->wrapper[2] = (uintptr_t)(uint8_t)Fast::OTR_G_RESETFB << 24;
    f3d->wrapper[4] = (uintptr_t)0xDF << 24; // G_ENDDL
    auto& gfx = *f3d->interpreter;
    gfx.StartFrame();
    gfx.Run((Gfx*)f3d->wrapper, replacements);
    // No flip here: a task draws into the game's colour images; the window shows what the VI presents.
    f3d->renderer->EndFrame();
    f3d->renderer->FinishRender();
    return (int)stats->commands;
}
extern "C" int n64c_f3d_run(N64cF3d* f3d, const N64cGfxMem* mem, uint32_t dl, N64cDlStats* stats)
{
    FpMasked fp_masked;
    auto context = f3d->window->UseContext();
    n64c_dl_arena_reset(&f3d->arena);
    f3d->surface = 0;
    f3d->in_sub = false;
    TaskRec task = { dl, f3d->arena.other_mode_h, f3d->arena.other_mode_h_known, f3d->arena.scissor_lry, 0, 0,
                     f3d->pending.mtx.size(), 0 };
    f3d->capture = f3d->unlock ? &f3d->pending.mtx : nullptr;
    if (f3d->unlock) {
        f3d->rdp_start = *f3d->interpreter->mRdp;
        f3d->rsp_start = *f3d->interpreter->mRsp;
    }
    check_textures(f3d, mem, false);
    const int commands = render(f3d, mem, dl, &f3d->arena, stats, nullptr);
    check_textures(f3d, mem, true);
    f3d->capture = nullptr;
    if (f3d->unlock) {
        f3d->rdp_end = *f3d->interpreter->mRdp;
        f3d->rsp_end = *f3d->interpreter->mRsp;
    }
    if (f3d->unlock && commands > 0) {
        // The game runs on after this task returns and may rewrite what the list pointed at before the retrace ends:
        // keep the memory as this task saw it (only a game with a base address for memory can be copied).
        f3d->snapshot_mem = {};
        if (mem->base && !mem->replay) {
            f3d->snapshot.assign(mem->base, mem->base + (size_t)mem->mask + 1);
            f3d->snapshot_mem = { f3d->snapshot.data(), mem->mask, 0 };
        }
        task.commands = stats->commands;
        task.lists = stats->lists;
        task.mtx_end = f3d->pending.mtx.size();
        f3d->pending.tasks.push_back(task);
    }
    return commands;
}
extern "C" void n64c_f3d_unlock(N64cF3d* f3d, int on)
{
    FpMasked fp_masked;
    f3d->unlock = on != 0;
    f3d->pending = {};
    f3d->cur = {};
    f3d->prev = {};
    f3d->have_cur = f3d->interp = false;
}
// Remove one texture cache entry and unbind it, so its next use decodes its source again.
static Fast::TextureCacheMap::iterator drop_texture(N64cF3d* f3d, Fast::TextureCacheMap::iterator it)
{
    auto& cache = f3d->interpreter->mTextureCache;
    for (auto& bound : f3d->interpreter->mRenderingState.mTextures)
        if (bound == &*it)
            bound = nullptr;
    cache.lru.erase(it->second.lru_location);
    cache.free_texture_ids.push_back(it->second.texture_id);
    return cache.map.erase(it);
}
// Fast3D caches textures by address, and the copy of game memory has the same addresses for every frame: before the
// game's next tasks run, drop the cache entries the interpolated draws made, so the cache is what it would be without
// them (a texture the game rewrote in place is reloaded for the next frame's draws, and the game's own pictures are
// the same with the unlock on or off).
static void purge_snapshot_textures(N64cF3d* f3d)
{
    if (f3d->snapshot.empty())
        return;
    const uint8_t *lo = f3d->snapshot.data(), *hi = lo + f3d->snapshot.size();
    auto& cache = f3d->interpreter->mTextureCache;
    for (auto it = cache.map.begin(); it != cache.map.end();)
        it = it->first.texture_addr >= lo && it->first.texture_addr < hi ? drop_texture(f3d, it) : std::next(it);
}
// The game memory a key was decoded from (game memory as n64c_dl maps it): its pixels, then each palette half a
// TLUT load filled (256 bytes at most). False when the pixels are not game memory (n64c_dl's private views).
static bool texture_source(const N64cGfxMem* mem, const Fast::TextureCacheKey& key, std::vector<uint8_t>* out)
{
    const uint8_t* lo = mem->base ? mem->base : (const uint8_t*)(uintptr_t)0x80000000u;
    const uint8_t* hi = lo + (size_t)mem->mask + 1;
    if (key.texture_addr < lo || key.texture_addr >= hi)
        return false;
    out->clear();
    auto add = [&](const uint8_t* p, size_t n) {
        if (p >= lo && p < hi)
            out->insert(out->end(), p, p + std::min(n, (size_t)(hi - p)));
    };
    add(key.texture_addr, key.size_bytes);
    for (const uint8_t* palette : key.palette_addrs)
        add(palette, 256);
    return true;
}
// Fast3D also caches the game's own textures by the address of their pixels and palettes, but the RDP reads RDRAM at
// every load and a game may rewrite an image in place between tasks (a menu sets its text into glyph images: the
// options menu showed SKIKS for SKINS). Before a task, drop the entries whose source changed since they were decoded;
// after it, keep the source of the entries it decoded.
static void check_textures(N64cF3d* f3d, const N64cGfxMem* mem, bool after)
{
    auto& cache = f3d->interpreter->mTextureCache;
    std::vector<uint8_t> now;
    if (after) {
        for (auto& entry : cache.map)
            if (!f3d->decoded.count(entry.first) && texture_source(mem, entry.first, &now))
                f3d->decoded.emplace(entry.first, now);
        return;
    }
    for (auto it = f3d->decoded.begin(); it != f3d->decoded.end();) {
        auto entry = cache.map.find(it->first);
        const bool same = entry != cache.map.end() && texture_source(mem, it->first, &now) && now == it->second;
        if (same) {
            ++it;
            continue;
        }
        if (entry != cache.map.end())
            drop_texture(f3d, entry);
        it = f3d->decoded.erase(it);
    }
}
extern "C" void n64c_f3d_group_begin(N64cF3d* f3d)
{
    FpMasked fp_masked;
    f3d->pending = {};
    if (f3d->unlock) {
        auto context = f3d->window->UseContext();
        purge_snapshot_textures(f3d);
    }
}
// The two groups draw the same scene when every task has the same list and command counts and the matrices are the
// same sequence of kinds; then the matrix at one position in one is the same object's in the other.
static bool same_shape(const Group& a, const Group& b)
{
    if (a.tasks.size() != 1 || b.tasks.size() != 1 || a.mtx.size() != b.mtx.size() || a.mtx.empty())
        return false;
    for (size_t i = 0; i < a.tasks.size(); i++)
        if (a.tasks[i].commands != b.tasks[i].commands || a.tasks[i].lists != b.tasks[i].lists ||
            a.tasks[i].mtx_begin != b.tasks[i].mtx_begin)
            return false;
    for (size_t i = 0; i < a.mtx.size(); i++)
        if (a.mtx[i].params != b.mtx[i].params)
            return false;
    return true;
}
extern "C" int n64c_f3d_group_end(N64cF3d* f3d)
{
    FpMasked fp_masked;
    if (f3d->pending.tasks.empty())
        return 0;
    const bool had = f3d->have_cur;
    f3d->prev = std::move(f3d->cur);
    f3d->cur = std::move(f3d->pending);
    f3d->cur.surface = f3d->surface;
    f3d->pending = {};
    f3d->have_cur = true;
    f3d->interp = had && f3d->snapshot_mem.base && same_shape(f3d->prev, f3d->cur);
    return 1 | (f3d->interp ? 2 : 0);
}
// One matrix between its previous and current value. A matrix that turned by more than about 20 degrees or changed
// scale by a third between two game frames is a cut, a spawn or a different object: it keeps its current value.
static MtxF lerp_matrix(const MtxF& a, const MtxF& b, float t)
{
    float scale = 1e-3f, delta = 0;
    for (int i = 0; i < 3; i++)
        for (int j = 0; j < 3; j++) {
            scale = std::max(scale, std::max(std::fabs(a.mf[i][j]), std::fabs(b.mf[i][j])));
            delta = std::max(delta, std::fabs(b.mf[i][j] - a.mf[i][j]));
        }
    if (delta > 0.35f * scale)
        return b;
    MtxF m;
    for (int i = 0; i < 4; i++)
        for (int j = 0; j < 4; j++)
            m.mf[i][j] = a.mf[i][j] * (1.0f - t) + b.mf[i][j] * t; // exact at t = 0 and 1
    return m;
}
extern "C" int n64c_f3d_interp_draw(N64cF3d* f3d, float alpha)
{
    FpMasked fp_masked;
    if (!f3d->unlock || !f3d->have_cur || !f3d->interp)
        return 0;
    auto context = f3d->window->UseContext();
    const TaskRec& task = f3d->cur.tasks.front();
    f3d->in_sub = true;
    f3d->sub_surface = 0;
    n64c_dl_arena_reset(&f3d->sub_arena);
    f3d->sub_arena.other_mode_h = task.other_mode_h;
    f3d->sub_arena.other_mode_h_known = task.other_mode_h_known;
    f3d->sub_arena.scissor_lry = task.scissor_lry;
    N64cDlStats stats = {};
    const SubPlan plan = { &f3d->prev, &f3d->cur, alpha };
    *f3d->interpreter->mRdp = f3d->rdp_start;
    *f3d->interpreter->mRsp = f3d->rsp_start;
    const int drawn = render(f3d, &f3d->snapshot_mem, task.dl, &f3d->sub_arena, &stats, &plan);
    *f3d->interpreter->mRdp = f3d->rdp_end;
    *f3d->interpreter->mRsp = f3d->rsp_end;
    f3d->in_sub = false;
    return drawn > 0 && !stats.unknown && f3d->sub_surface;
}
extern "C" uint32_t n64c_f3d_surface(const N64cF3d* f3d) { return f3d->surface; }
static int read_surface(N64cF3d* f3d, const std::map<uint32_t, int>& surfaces, uint32_t phys, uint32_t* rgba,
                        uint32_t w, uint32_t h, uint32_t rows)
{
    auto context = f3d->window->UseContext();
    const auto it = surfaces.find(phys);
    if (it == surfaces.end() || !rgba || !w || !h)
        return 0;
    const auto info = f3d->interpreter->mFrameBuffers.find(it->second);
    if (info == f3d->interpreter->mFrameBuffers.end())
        return 0;
    const uint32_t sw = info->second.applied_width, full = info->second.applied_height;
    std::vector<uint16_t> src((size_t)sw * full);
    f3d->renderer->ReadFramebufferToCPU(it->second, sw, full, src.data()); // RGBA 5551, top row first
    // The rows the VI shows, counted in the render target's rows (it may be drawn at a scaled size).
    const uint32_t orig = info->second.orig_height;
    const uint32_t sh =
        rows && orig > rows ? std::max<uint32_t>(1, (uint32_t)std::lround((double)full * rows / orig)) : full;
    auto channel = [&](uint32_t x, uint32_t y, int c) {
        const uint16_t p = src[(size_t)y * sw + x];
        const uint32_t v = c == 3 ? (p & 1) * 255u : (p >> (11 - 5 * c) & 31);
        return c == 3 ? (float)v : (float)(v << 3 | v >> 2);
    };
    for (uint32_t y = 0; y < h; y++) {
        const float fy = std::max(0.0f, ((float)y + 0.5f) * sh / h - 0.5f);
        const uint32_t y0 = std::min((uint32_t)fy, sh - 1), y1 = std::min(y0 + 1, sh - 1);
        const float ty = fy - (float)y0;
        for (uint32_t x = 0; x < w; x++) {
            const float fx = std::max(0.0f, ((float)x + 0.5f) * sw / w - 0.5f);
            const uint32_t x0 = std::min((uint32_t)fx, sw - 1), x1 = std::min(x0 + 1, sw - 1);
            const float tx = fx - (float)x0;
            uint32_t out = 0;
            for (int c = 0; c < 4; c++) {
                const float top = channel(x0, y0, c) * (1 - tx) + channel(x1, y0, c) * tx;
                const float bottom = channel(x0, y1, c) * (1 - tx) + channel(x1, y1, c) * tx;
                out |= (uint32_t)std::lround(top * (1 - ty) + bottom * ty) << (8 * c);
            }
            rgba[(size_t)y * w + x] = out;
        }
    }
    return 1;
}
extern "C" int n64c_f3d_read(N64cF3d* f3d, uint32_t phys, uint32_t* rgba, uint32_t w, uint32_t h, uint32_t rows)
{
    FpMasked fp_masked;
    return read_surface(f3d, f3d->surfaces, phys, rgba, w, h, rows);
}
// Debug check: the interpolated draw at alpha 1 must be the game's own picture of the same frame.
extern "C" int n64c_f3d_interp_check(N64cF3d* f3d, uint32_t w, uint32_t h, uint32_t* drawn_out, uint32_t* game_out)
{
    FpMasked fp_masked;
    if (!f3d->sub_surface || !f3d->cur.surface || !w || !h)
        return -1;
    std::vector<uint32_t> drawn((size_t)w * h), game((size_t)w * h);
    if (!read_surface(f3d, f3d->sub_surfaces, f3d->sub_surface, drawn.data(), w, h, 0) ||
        !read_surface(f3d, f3d->surfaces, f3d->cur.surface, game.data(), w, h, 0))
        return -1;
    if (drawn_out && game_out) {
        std::copy(drawn.begin(), drawn.end(), drawn_out);
        std::copy(game.begin(), game.end(), game_out);
    }
    int differing = 0;
    for (size_t i = 0; i < drawn.size(); i++)
        for (int c = 0; c < 3; c++)
            if (std::abs((int)(drawn[i] >> (8 * c) & 255) - (int)(game[i] >> (8 * c) & 255)) > 2) {
                differing++;
                break;
            }
    if (differing)
        std::fprintf(stderr, "f3d-check: differing=%d game surface 0x%08x drawn surface 0x%08x tasks=%zu matrices=%zu\n",
                     differing, f3d->cur.surface, f3d->sub_surface, f3d->cur.tasks.size(), f3d->cur.mtx.size());
    return differing;
}
extern "C" uint32_t n64c_f3d_refresh_hz(N64cF3d* f3d)
{
    FpMasked fp_masked;
    uint32_t hz = 0;
    f3d->window->GetActiveWindowRefreshRate(&hz);
    return hz;
}
extern "C" int n64c_f3d_interp_read(N64cF3d* f3d, uint32_t* rgba, uint32_t w, uint32_t h)
{
    FpMasked fp_masked;
    return f3d->sub_surface && read_surface(f3d, f3d->sub_surfaces, f3d->sub_surface, rgba, w, h, 0);
}
// The port's own 5x7 status font: printable ASCII 0x20-0x5f, one byte per row, bit 4 the leftmost
// column. Lower case is drawn upper case. Hand-drawn for this file.
static const uint8_t status_font[64][7] = {
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}, {0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04}, // space !
    {0x0a, 0x0a, 0x00, 0x00, 0x00, 0x00, 0x00}, {0x0a, 0x0a, 0x1f, 0x0a, 0x1f, 0x0a, 0x0a}, // " #
    {0x04, 0x0f, 0x14, 0x0e, 0x05, 0x1e, 0x04}, {0x18, 0x19, 0x02, 0x04, 0x08, 0x13, 0x03}, // $ %
    {0x0c, 0x12, 0x14, 0x08, 0x15, 0x12, 0x0d}, {0x04, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00}, // & '
    {0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02}, {0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08}, // ( )
    {0x00, 0x04, 0x15, 0x0e, 0x15, 0x04, 0x00}, {0x00, 0x04, 0x04, 0x1f, 0x04, 0x04, 0x00}, // * +
    {0x00, 0x00, 0x00, 0x00, 0x0c, 0x04, 0x08}, {0x00, 0x00, 0x00, 0x1f, 0x00, 0x00, 0x00}, // , -
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x0c, 0x0c}, {0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x00}, // . /
    {0x0e, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0e}, {0x04, 0x0c, 0x04, 0x04, 0x04, 0x04, 0x0e}, // 0 1
    {0x0e, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1f}, {0x1f, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0e}, // 2 3
    {0x02, 0x06, 0x0a, 0x12, 0x1f, 0x02, 0x02}, {0x1f, 0x10, 0x1e, 0x01, 0x01, 0x11, 0x0e}, // 4 5
    {0x06, 0x08, 0x10, 0x1e, 0x11, 0x11, 0x0e}, {0x1f, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08}, // 6 7
    {0x0e, 0x11, 0x11, 0x0e, 0x11, 0x11, 0x0e}, {0x0e, 0x11, 0x11, 0x0f, 0x01, 0x02, 0x0c}, // 8 9
    {0x00, 0x0c, 0x0c, 0x00, 0x0c, 0x0c, 0x00}, {0x00, 0x0c, 0x0c, 0x00, 0x0c, 0x04, 0x08}, // : ;
    {0x02, 0x04, 0x08, 0x10, 0x08, 0x04, 0x02}, {0x00, 0x00, 0x1f, 0x00, 0x1f, 0x00, 0x00}, // < =
    {0x08, 0x04, 0x02, 0x01, 0x02, 0x04, 0x08}, {0x0e, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04}, // > ?
    {0x0e, 0x11, 0x01, 0x0d, 0x15, 0x15, 0x0e}, {0x0e, 0x11, 0x11, 0x11, 0x1f, 0x11, 0x11}, // @ A
    {0x1e, 0x11, 0x11, 0x1e, 0x11, 0x11, 0x1e}, {0x0e, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0e}, // B C
    {0x1c, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1c}, {0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x1f}, // D E
    {0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x10}, {0x0e, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0f}, // F G
    {0x11, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11}, {0x0e, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0e}, // H I
    {0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0c}, {0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11}, // J K
    {0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1f}, {0x11, 0x1b, 0x15, 0x15, 0x11, 0x11, 0x11}, // L M
    {0x11, 0x11, 0x19, 0x15, 0x13, 0x11, 0x11}, {0x0e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e}, // N O
    {0x1e, 0x11, 0x11, 0x1e, 0x10, 0x10, 0x10}, {0x0e, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0d}, // P Q
    {0x1e, 0x11, 0x11, 0x1e, 0x14, 0x12, 0x11}, {0x0f, 0x10, 0x10, 0x0e, 0x01, 0x01, 0x1e}, // R S
    {0x1f, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04}, {0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e}, // T U
    {0x11, 0x11, 0x11, 0x11, 0x11, 0x0a, 0x04}, {0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0a}, // V W
    {0x11, 0x11, 0x0a, 0x04, 0x0a, 0x11, 0x11}, {0x11, 0x11, 0x11, 0x0a, 0x04, 0x04, 0x04}, // X Y
    {0x1f, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1f}, {0x0e, 0x08, 0x08, 0x08, 0x08, 0x08, 0x0e}, // Z [
    {0x00, 0x10, 0x08, 0x04, 0x02, 0x01, 0x00}, {0x0e, 0x02, 0x02, 0x02, 0x02, 0x02, 0x0e}, // backslash ]
    {0x04, 0x0a, 0x11, 0x00, 0x00, 0x00, 0x00}, {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1f}, // ^ _
};
// One solid rectangle in window pixels (top-left origin) on the bound window framebuffer.
static void status_fill(uint32_t wh, int x, int y, int w, int h, float r, float g, float b)
{
    glScissor(x, (int)wh - y - h, w, h);
    glClearColor(r, g, b, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
}
// text (lines split by newlines) on a dark panel at the top left, scaled to fit the window.
static void status_text(uint32_t ww, uint32_t wh, const char* text, int selected = -1, int max_scale = 4, bool center = false)
{
    int lines = 1, cols = 0, col = 0;
    for (const char* c = text; *c; c++) {
        if (*c == '\n') {
            lines++;
            col = 0;
            continue;
        }
        cols = std::max(cols, ++col);
    }
    if (!cols)
        return;
    // A cell is 6x9 font pixels (glyph plus spacing); the panel has one cell of margin all round.
    const int scale = std::max(1, std::min({ max_scale, (int)ww / ((cols + 2) * 6), (int)wh / ((lines + 2) * 9) }));
    const int cw = 6 * scale, ch = 9 * scale;
    const GLboolean scissor = glIsEnabled(GL_SCISSOR_TEST);
    GLint box[4];
    glGetIntegerv(GL_SCISSOR_BOX, box);
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    glEnable(GL_SCISSOR_TEST);
    const int ox = center ? std::max(0, ((int)ww - (cols + 2) * cw) / 2) : 0;
    const int oy = center ? std::max(0, ((int)wh - (lines + 2) * ch) / 2) : 0;
    status_fill(wh, ox, oy, (cols + 2) * cw, (lines + 2) * ch, 0.08f, 0.08f, 0.12f);
    if (selected >= 0)
        status_fill(wh, ox, oy + (1 + selected) * ch, (cols + 2) * cw, ch, 0.22f, 0.22f, 0.42f);
    int x = ox + cw, y = oy + ch, line = 0;
    for (const char* c = text; *c; c++) {
        if (*c == '\n') {
            x = ox + cw;
            y += ch;
            line++;
            continue;
        }
        unsigned code = (unsigned char)*c;
        if (code >= 'a' && code <= 'z')
            code -= 'a' - 'A';
        if (code < 0x20 || code > 0x5f)
            code = '?';
        for (int row = 0; row < 7; row++)
            for (int bit = 0; bit < 5; bit++)
                if (status_font[code - 0x20][row] & (0x10 >> bit))
                    status_fill(wh, x + bit * scale, y + row * scale, scale, scale, line == selected ? 1.0f : 1.0f,
                                line == selected ? 1.0f : 0.75f, line == selected ? 1.0f : 0.2f);
        x += cw;
    }
    glScissor(box[0], box[1], box[2], box[3]);
    if (!scissor)
        glDisable(GL_SCISSOR_TEST);
}
// The window's picture as a binary PPM (rows top to bottom), for the options screen's evidence.
static void write_window_shot(const char* path, uint32_t w, uint32_t h)
{
    std::vector<uint8_t> rgba((size_t)w * h * 4);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadPixels(0, 0, (GLsizei)w, (GLsizei)h, GL_RGBA, GL_UNSIGNED_BYTE, rgba.data());
    FILE* f = fopen(path, "wb");
    if (!f)
        return;
    fprintf(f, "P6\n%u %u\n255\n", w, h);
    for (uint32_t y = h; y-- > 0;)
        for (uint32_t x = 0; x < w; x++)
            fwrite(&rgba[((size_t)y * w + x) * 4], 1, 3, f);
    fclose(f);
}
static int present(N64cF3d* f3d, const std::map<uint32_t, int>& surfaces, uint32_t phys, const char* text, bool events)
{
    auto context = f3d->window->UseContext();
    if (events)
        f3d->window->HandleEvents();
    uint32_t ww = 0, wh = 0;
    f3d->window->GetOutputDimensions(&ww, &wh);
    f3d->renderer->UpdateFramebufferParameters(0, ww, wh, 1, false, true, true, true);
    f3d->renderer->StartDrawToFramebuffer(0, 1);
    f3d->renderer->ClearFramebuffer(true, true);
    const auto it = phys ? surfaces.find(phys) : surfaces.end();
    if (it != surfaces.end()) {
        const auto info = f3d->interpreter->mFrameBuffers.find(it->second);
        if (info != f3d->interpreter->mFrameBuffers.end())
        {
            int dx = 0, dy = 0, dw = (int)ww, dh = (int)wh;
#ifdef __ANDROID__
            // The picture is 4:3; a phone screen is wider. Keep the aspect and leave bars (cleared above).
            if ((uint64_t)ww * 3 > (uint64_t)wh * 4) {
                dw = (int)((uint64_t)wh * 4 / 3);
                dx = ((int)ww - dw) / 2;
            } else {
                dh = (int)((uint64_t)ww * 3 / 4);
                dy = ((int)wh - dh) / 2;
            }
#endif
            f3d->renderer->CopyFramebuffer(0, it->second, 0, 0, (int)info->second.applied_width,
                                           (int)info->second.applied_height, dx, dy, dx + dw, dy + dh);
        }
    }
    if (text) {
        f3d->renderer->StartDrawToFramebuffer(0, 1); // CopyFramebuffer may leave its source bound
        status_text(ww, wh, text);
    } else if (!f3d->overlay.empty()) {
        f3d->renderer->StartDrawToFramebuffer(0, 1);
        status_text(ww, wh, f3d->overlay.c_str(), f3d->overlay_selected, f3d->overlay_scale, f3d->overlay_selected >= 0);
    }
    f3d->renderer->EndFrame();
    if (!f3d->window_shot.empty())
        write_window_shot(f3d->window_shot.c_str(), ww, wh), f3d->window_shot.clear();
    f3d->window->SwapBuffersBegin();
    return f3d->window->IsRunning() ? 1 : 0;
}
extern "C" int n64c_f3d_present(N64cF3d* f3d, uint32_t phys)
{
    FpMasked fp_masked;
    return present(f3d, f3d->surfaces, phys, nullptr, true);
}
// The interpolated draw's picture, with no event poll: input is read once per VI present as ever.
extern "C" int n64c_f3d_interp_present(N64cF3d* f3d)
{
    FpMasked fp_masked;
    return present(f3d, f3d->sub_surfaces, f3d->sub_surface, nullptr, false);
}
extern "C" int n64c_f3d_events(N64cF3d* f3d)
{
    FpMasked fp_masked;
    auto context = f3d->window->UseContext();
    f3d->window->HandleEvents();
    return f3d->window->IsRunning() ? 1 : 0;
}
extern "C" int n64c_f3d_present_text(N64cF3d* f3d, uint32_t phys, const char* text)
{
    FpMasked fp_masked;
    return present(f3d, f3d->surfaces, phys, text ? text : "", true);
}
extern "C" void n64c_f3d_overlay(N64cF3d* f3d, const char* text, int selected_line, int max_scale)
{
    f3d->overlay = text ? text : "";
    f3d->overlay_selected = selected_line;
    f3d->overlay_scale = max_scale < 1 ? 1 : max_scale > 4 ? 4 : max_scale;
}
extern "C" void n64c_f3d_window_shot(N64cF3d* f3d, const char* path) { f3d->window_shot = path ? path : ""; }
extern "C" void n64c_f3d_window_mode(N64cF3d* f3d, int mode, uint32_t width, uint32_t height)
{
    FpMasked fp_masked;
    auto context = f3d->window->UseContext();
    f3d->window->SetMode(mode, width, height);
}
extern "C" void n64c_f3d_render_scale(N64cF3d* f3d, int percent) { f3d->window->SetRenderScale(percent); }
extern "C" size_t n64c_f3d_display_sizes(N64cF3d* f3d, uint32_t (*sizes)[2], size_t max)
{
    return f3d->window->DisplaySizes(sizes, max);
}
extern "C" void n64c_f3d_quit(N64cF3d* f3d) { f3d->window->Close(); }
extern "C" void n64c_f3d_open_controls(N64cF3d*) { n64c_controls_open(); }
extern "C" const char* n64c_f3d_renderer(const N64cF3d* f3d) { return f3d->gl_renderer.c_str(); }

extern "C" void n64c_f3d_close(N64cF3d* f3d)
{
    FpMasked fp_masked;
    if (!f3d)
        return;
    auto context = f3d->window->UseContext();
    n64c_dl_arena_free(&f3d->arena);
    n64c_dl_arena_free(&f3d->sub_arena);
    f3d->interpreter->Destroy();
    f3d->interpreter.reset();
    delete f3d->renderer;
    context.Retire();
    delete f3d->window;
    delete f3d;
}
