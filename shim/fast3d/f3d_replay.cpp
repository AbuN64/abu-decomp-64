// SPDX-License-Identifier: GPL-3.0-or-later
// f3d_replay: render a captured display list with the Fast3D renderer and no game code.
// usage: n64c_f3d_replay <rdram.bin> <dl-address> <out.ppm> [fbw fbh [outw outh]]
// tests/replay_ssim.py compares the result with the oracle frame of the capture. fbw/fbh are the game
// framebuffer size (default 320x240), outw/outh the output (default 320x240). The output is the colour
// image the list drew to last. Fast3D's shader sources are read from fast3d-shaders.o2r beside the executable.
#include "n64c_f3d.h"
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <vector>

int main(int argc, char** argv)
{
    if (argc < 4) {
        std::fprintf(stderr, "usage: %s rdram.bin dl-address out.ppm [fbw fbh [outw outh]]\n", argv[0]);
        return 2;
    }
    const int nargs = argc - 1;
    const uint32_t fbw = nargs >= 5 ? (uint32_t)std::atoi(argv[4]) : 320, fbh = nargs >= 5 ? (uint32_t)std::atoi(argv[5]) : 240;
    const uint32_t ow = nargs >= 7 ? (uint32_t)std::atoi(argv[6]) : 320, oh = nargs >= 7 ? (uint32_t)std::atoi(argv[7]) : 240;
    std::FILE* f = std::fopen(argv[1], "rb");
    if (!f) {
        std::perror(argv[1]);
        return 2;
    }
    std::vector<uint8_t> ram(8u << 20);
    const size_t got = std::fread(ram.data(), 1, ram.size(), f);
    std::fclose(f);
    if (got != ram.size()) {
        std::fprintf(stderr, "rdram.bin must be 8 MiB\n");
        return 2;
    }
    const std::string settings = (std::filesystem::path(argv[3]).parent_path() / "f3d-replay.json").string();
    const N64cF3dConfig config = {nullptr, settings.c_str(), ow, oh, fbw, fbh, 1, 1};
    N64cF3d* f3d = n64c_f3d_open(&config);
    if (!f3d)
        return 1;
    std::fprintf(stderr, "f3d_replay: GL %s\n", n64c_f3d_renderer(f3d));
    const N64cGfxMem mem = {ram.data(), 0x7FFFFF, 1};
    N64cDlStats stats = {};
    if (n64c_f3d_run(f3d, &mem, (uint32_t)std::strtoul(argv[2], nullptr, 0), &stats) < 0)
        return 1;
    const uint32_t surface = n64c_f3d_surface(f3d);
    std::fprintf(stderr,
                 "f3d_replay: %u lists, %u commands, %u microcode loads, %u unresolved, %u lines (%u dropped), "
                 "%u unknown, surface 0x%08x\n",
                 stats.lists, stats.commands, stats.ucode_loads, stats.bad, stats.lines, stats.lines_dropped,
                 stats.unknown, surface);
    std::vector<uint32_t> rgba((size_t)ow * oh);
    if (!n64c_f3d_read(f3d, surface, rgba.data(), ow, oh))
        return 1;
    std::FILE* o = std::fopen(argv[3], "wb");
    if (!o) {
        std::perror(argv[3]);
        return 2;
    }
    std::fprintf(o, "P6\n%u %u\n255\n", ow, oh);
    for (uint32_t c : rgba) {
        const unsigned char px[3] = {(unsigned char)c, (unsigned char)(c >> 8), (unsigned char)(c >> 16)};
        std::fwrite(px, 1, 3, o);
    }
    const bool ok = std::fclose(o) == 0;
    n64c_f3d_close(f3d);
    return ok ? 0 : 2;
}
