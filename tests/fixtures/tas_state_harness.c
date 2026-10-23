/* Test harness for shim/tas_state.h: prints the trace line of one or two polls and writes the canonical dump of the first.
 * usage: harness SPEC RAM.bin DUMP.bin [RAM2.bin]   (compile with -DTS_ORACLE for the original's word-swizzled RDRAM) */
#include "../../shim/tas_state.h"

static uint8_t *load(const char *path, uint32_t bytes)
{
    uint8_t *ram = calloc(1, 0x4000000);
    FILE *in = fopen(path, "rb");
    if (!in || !fread(ram, 1, bytes, in)) exit(3);
    fclose(in);
    return ram;
}

int main(int argc, char **argv)
{
    struct ts_spec spec;
    if (argc < 4 || ts_load(argv[1], &spec)) return 2;
    uint64_t *now = calloc(spec.nsymbols, sizeof *now);
    uint32_t *prev = calloc(spec.nsymbols, sizeof *prev);
    int started = 0;
    uint8_t *ram = load(argv[2], spec.ram_bytes);
    ts_hash(&spec, ram, now);
    ts_trace_line(stdout, 1, &spec, now, prev, &started);
    FILE *out = fopen(argv[3], "wb");
    if (!out || ts_dump(&spec, ram, out) || fclose(out)) return 4;
    if (argc > 4) {
        ts_hash(&spec, load(argv[4], spec.ram_bytes), now);
        ts_trace_line(stdout, 2, &spec, now, prev, &started);
    }
    return 0;
}
