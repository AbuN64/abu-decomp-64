/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Native driver: project facts and link bindings are generated from the input
 * manifest. No guest code, data layout or hardware address is inferred here. */
#include "boot.h"
#include "frame.h"
#include "trace.h"
#include "sched_glue.h"
#include <errno.h>
#include <limits.h>
#include "common/com_sha256.h"
#include "n64c_memory.h"
#include "n64c_internal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int positive(const char *text,u32 *value)
{
    char *end;
    errno=0;
    unsigned long parsed=strtoul(text,&end,10);
    if (errno || !*text || *end || *text=='-' || !parsed || parsed>UINT32_MAX) return 0;
    *value=(u32)parsed;
    return 1;
}
int main(int argc,char **argv)
{
    /* Capture: run to RETRACE and write the presented frame. Live (--window): show every presentation. */
    const int live=argc==7 && strcmp(argv[1],"--window")==0;
    if (!live && argc!=8) {
        fprintf(stderr,"usage: %s ROM.z64 FRAME.ppm RETRACE FB_WIDTH FB_HEIGHT OUT_WIDTH OUT_HEIGHT\n"
                "       %s --window ROM.z64 FB_WIDTH FB_HEIGHT WINDOW_WIDTH WINDOW_HEIGHT\n",argv[0],argv[0]);
        return 2;
    }
    const char *rom_path=argv[live ? 2 : 1];
    char **geometry=argv+(live ? 3 : 4);
    u32 retrace=0,fb_width,fb_height,out_width,out_height;
    if ((!live && !positive(argv[3],&retrace)) || !positive(geometry[0],&fb_width) ||
        !positive(geometry[1],&fb_height) || !positive(geometry[2],&out_width) ||
        !positive(geometry[3],&out_height)) {
        fprintf(stderr,"boot-input: invalid frame configuration\n");
        return 2;
    }
    FILE *rom=fopen(rom_path,"rb");
    if (!rom || fseek(rom,0,SEEK_END)!=0) {
        if (rom) fclose(rom);
        fprintf(stderr,"boot-input: cannot read ROM\n");
        return 2;
    }
    long bytes=ftell(rom);
    if (bytes<=0 || (unsigned long)bytes!=n64c_boot_rom_bytes || fseek(rom,0,SEEK_SET)!=0) {
        fclose(rom);
        fprintf(stderr,"boot-input: invalid ROM extent\n");
        return 2;
    }
    unsigned char *image=malloc((size_t)bytes);
    if (!image || fread(image,1,(size_t)bytes,rom)!=(size_t)bytes) {
        free(image);
        fclose(rom);
        fprintf(stderr,"boot-input: incomplete ROM\n");
        return 2;
    }
    fclose(rom);
    char digest[65];
    Com_SHA256(image,(size_t)bytes,digest);
    free(image);
    if (strcmp(digest,n64c_boot_rom_sha256)!=0) {
        fprintf(stderr,"boot-input: ROM digest differs from project input\n");
        return 2;
    }
    N64cConfig config={.rom_path=rom_path};
    n64c_init(&config);
    for (size_t i=0;i<n64c_boot_load_count;i++) {
        const N64cBootLoad *load=&n64c_boot_loads[i];
        if (load->rom>(u32)bytes || load->bytes>(u32)bytes-load->rom ||
            n64c_pi_register(load->rom,load->rom+load->bytes,load->kind,load->fields)!=0) {
            fprintf(stderr,"boot-input: invalid ROM layout %s\n",load->symbol);
            n64c_shutdown();
            return 2;
        }
    }
    for (size_t i=0;i<n64c_boot_resource_count;i++) {
        const N64cBootLoad *resource=&n64c_boot_resources[i];
        if (resource->rom>(u32)bytes || resource->bytes>(u32)bytes-resource->rom ||
            n64c_pi_register(resource->rom,resource->rom+resource->bytes,resource->kind,resource->fields)!=0) {
            fprintf(stderr,"boot-input: invalid ROM resource %s\n",resource->symbol);
            n64c_shutdown();
            return 2;
        }
    }
    for (size_t i=0;i<n64c_boot_packed_count;i++) {
        const N64cBootLoad *stream=&n64c_boot_packed[i];
        if (stream->rom>(u32)bytes || stream->bytes>(u32)bytes-stream->rom ||
            n64c_pi_register_packed(stream->rom,stream->rom+stream->bytes,stream->kind,stream->fields)!=0) {
            fprintf(stderr,"boot-input: invalid RNC stream %s\n",stream->symbol);
            n64c_shutdown();
            return 2;
        }
    }
    for (size_t i=0;i<n64c_boot_rom_image_count;i++) {
        const N64cBootRomImage *part=&n64c_boot_rom_image[i];
        if (part->rom>(u32)bytes || part->bytes>(u32)bytes-part->rom ||
            n64c_pi_register_image(part->rom,part->rom+part->bytes,n64c_boot_image_bytes+part->offset)!=0) {
            fprintf(stderr,"boot-input: invalid overlay data image at ROM 0x%08x\n",part->rom);
            n64c_shutdown();
            return 2;
        }
    }
    N64cMemoryConfig memory={n64c_boot_ram_bytes,(u32)bytes,n64c_boot_video_clock};
    if (!n64c_memory_open(&memory)) {
        fprintf(stderr,"boot-input: native address windows unavailable\n");
        n64c_shutdown();
        return 2;
    }
    for (size_t i=0;i<n64c_boot_image_count;i++) {
        const N64cBootImage *part=&n64c_boot_image[i];
        void *destination=n64c_memory_ram(part->ram,part->bytes);
        if (!destination) {
            fprintf(stderr,"boot-input: data image outside RDRAM at 0x%08x\n",part->ram);
            n64c_shutdown();
            return 2;
        }
        memcpy(destination,n64c_boot_image_bytes+part->offset,part->bytes);
    }
    for (size_t i=0;i<n64c_boot_load_count;i++) {
        const N64cBootLoad *load=&n64c_boot_loads[i];
        void *destination=n64c_memory_ram(load->ram,load->bytes);
        if (!destination || n64c_pi_read(load->rom,destination,load->bytes)!=0) {
            fprintf(stderr,"boot-input: cannot load %s\n",load->symbol);
            n64c_shutdown();
            return 2;
        }
    }
    if (n64c_boot_system.initialized &&
        !n64c_system_bind(&n64c_boot_system,n64c_boot_tv_type,n64c_boot_reset_type,n64c_boot_ram_bytes)) {
        fprintf(stderr,"boot-input: invalid system globals\n");
        n64c_shutdown();
        return 2;
    }
    if (!n64c_boot_frame_start(fb_width,fb_height,n64c_boot_ram_bytes,n64c_boot_audio_abi,
                               live ? out_width : 0,live ? out_height : 0)) {
        fprintf(stderr,"boot-input: cannot configure native frame capture\n");
        n64c_shutdown();
        return 2;
    }
    n64c_game_scheduler_attach();
    if (n64c_trace_init) n64c_trace_init();
    fprintf(stderr,"boot-entry: original C\n");
    if (live) fprintf(stderr,"boot-step: original C\n");
    fflush(stderr);
    int result=n64c_boot_entry();
    int ok=!result && (live ? n64c_boot_frame_live() : n64c_boot_frame_capture(argv[2],retrace,out_width,out_height));
    n64c_boot_frame_close();
    n64c_shutdown();
    return ok ? 0 : 1;
}
