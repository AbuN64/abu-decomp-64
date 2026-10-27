/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The game executable: the user tree and its assets check (first run: the ROM dialog and extraction), then the
 * original C boots from the extracted cart. Project facts and link bindings are generated from the input manifest;
 * no guest code, data layout or hardware address is inferred here. */
#define _GNU_SOURCE
#include "n64c_prof.h"
#include "boot.h"
#include "frame.h"
#include "install/inst.h"
#include "install/inst_ui.h"
#include "n64c_input.h"
#include "n64c_internal.h"
#include "n64c_memory.h"
#include "os/n64c_host.h"
#include "sched_glue.h"
#include "trace.h"
#include <SDL3/SDL_filesystem.h>
#include <SDL3/SDL_stdinc.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

enum { EXIT_RUNTIME = 1, EXIT_USAGE = 2, EXIT_PROGRAM_TREE = 4 };

/* N64C_REPLAY=PLAN (replay.c, `decomp behave --replay`): run recorded function calls on the loaded image instead of booting. */
#ifdef _WIN32
int n64c_replay_run(const char *plan); /* replay.c: a stub on Windows */
#else
int n64c_replay_run(const char *plan) __attribute__((weak));
#endif

typedef struct {
    const char *rom, *user_dir, *capture, *pak, *cpak;
    int fps_unlock;
    int no_dialog, verify_assets, extract_only, offscreen, version, help;
    u32 retrace, fb_w, fb_h, out_w, out_h;
} Options;

static void usage(FILE *out, const char *name)
{
    fprintf(out,
            "usage: %s [--rom PATH] [--user-dir PATH] [--no-dialog] [--verify-assets] [--extract-only]\n"
            "          [--window WxH] [--fb WxH] [--capture FRAME.ppm --retrace N] [--offscreen] [--pak on|off] [--cpak off|PATH] [--version]\n"
            "  --rom PATH        your ROM of the game; extracted into the user folder when it is new\n"
            "  --user-dir PATH   user folder (assets, saves, config, logs) instead of the default one\n"
            "  --no-dialog       never open a window to ask for the ROM; explain on stderr and exit 3\n"
            "  --verify-assets   check the extracted game data and exit (0 ok, 3 missing or damaged)\n"
            "  --extract-only    prepare the game data and exit without starting the game\n"
            "  --window WxH      window size (default 640x480); with --capture, the picture size\n"
            "  --fb WxH          the game's framebuffer size (default 284x222)\n"
            "  --capture F --retrace N   run offscreen to retrace N and write the presented frame to F\n"
            "  --offscreen       run without a visible window\n"
            "  --fps-unlock      draw extra pictures between game frames (matrices interpolated) and present as fast as the host\n"
            "                    allows; the game's timing, audio and input are unchanged (also N64C_FPS_UNLOCK=1)\n"
            "  --cpak off|PATH   Controller Pak card file (32 KiB .mpk, made formatted when missing); default is controller-pak.mpk in the user folder (no Pak in a --capture run), off is no Pak\n"
            "  --pak on|off      Expansion Pak: 8 MB RDRAM (on) or the 4 MB console (off); default is the version's own\n"
            "exit: 0 ok, 1 runtime failure, 2 usage, 3 game data missing or ROM refused, 4 broken install\n",
            name);
}

static int positive(const char *text,u32 *value)
{
    char *end;
    errno=0;
    unsigned long parsed=text ? strtoul(text,&end,10) : 0;
    if (!text || errno || !*text || *end || *text=='-' || !parsed || parsed>4096) return 0;
    *value=(u32)parsed;
    return 1;
}

static int count(const char *text,u32 *value)
{
    char *end;
    errno=0;
    unsigned long parsed=strtoul(text,&end,10);
    if (errno || !*text || *end || *text=='-' || !parsed || parsed>UINT32_MAX) return 0;
    *value=(u32)parsed;
    return 1;
}

static int size_pair(const char *text,u32 *w,u32 *h)
{
    char a[16],b[16];
    const char *x=text ? strchr(text,'x') : NULL;
    if (!x || (size_t)(x-text)>=sizeof a || strlen(x+1)>=sizeof b) return 0;
    memcpy(a,text,(size_t)(x-text));
    a[x-text]=0;
    strcpy(b,x+1);
    return positive(a,w) && positive(b,h);
}

static int parse(int argc,char **argv,Options *o)
{
    *o=(Options){.fb_w=284,.fb_h=222,.out_w=640,.out_h=480};
    for (int i=1;i<argc;i++) {
        const char *arg=argv[i],*value=i+1<argc ? argv[i+1] : NULL;
        if (!strcmp(arg,"--no-dialog")) o->no_dialog=1;
        else if (!strcmp(arg,"--verify-assets")) o->verify_assets=1;
        else if (!strcmp(arg,"--extract-only")) o->extract_only=1;
        else if (!strcmp(arg,"--offscreen")) o->offscreen=1;
        else if (!strcmp(arg,"--fps-unlock")) o->fps_unlock=1;
        else if (!strcmp(arg,"--version")) o->version=1;
        else if (!strcmp(arg,"--help") || !strcmp(arg,"-h")) o->help=1;
        else if (!strcmp(arg,"--rom") && value) { o->rom=value;i++; }
        else if (!strcmp(arg,"--user-dir") && value) { o->user_dir=value;i++; }
        else if (!strcmp(arg,"--pak") && value) { o->pak=value;i++; }
        else if (!strcmp(arg,"--cpak") && value) { o->cpak=value;i++; }
        else if (!strcmp(arg,"--capture") && value) { o->capture=value;i++; }
        else if (!strcmp(arg,"--retrace") && value) {
            if (!count(value,&o->retrace)) {
                fprintf(stderr,"%s: --retrace takes a positive count\n",argv[0]);
                return 0;
            }
            i++;
        } else if ((!strcmp(arg,"--window") || !strcmp(arg,"--fb")) && value) {
            int fb=!strcmp(arg,"--fb");
            if (!size_pair(value,fb ? &o->fb_w : &o->out_w,fb ? &o->fb_h : &o->out_h)) {
                fprintf(stderr,"%s: %s takes WIDTHxHEIGHT (1..4096)\n",argv[0],arg);
                return 0;
            }
            i++;
        } else if (arg[0]!='-') {
            fprintf(stderr,"%s: the ROM is now passed with --rom PATH (see --help)\n",argv[0]);
            return 0;
        } else {
            fprintf(stderr,"%s: unknown or incomplete option %s (see --help)\n",argv[0],arg);
            return 0;
        }
    }
    u32 ram;
    if (!n64c_parse_pak(o->pak,0,&ram)) {
        fprintf(stderr,"%s: --pak takes on or off\n",argv[0]);
        return 0;
    }
    if (!o->capture!=!o->retrace) {
        fprintf(stderr,"%s: --capture and --retrace go together\n",argv[0]);
        return 0;
    }
    return 1;
}

/* stderr also goes to <user>/logs/<game>.log (os/n64c_host.h: nothing is lost when this process dies). */
static void tee_log(const char *user,const char *name)
{
    char logs[INST_PATH],path[INST_PATH];
    if (snprintf(logs,sizeof logs,"%s/logs",user)>=(int)sizeof logs ||
        snprintf(path,sizeof path,"%s/%s.log",logs,name)>=(int)sizeof path)
        return;
    inst_log_rotate(logs,name,5);
    n64c_host_tee_stderr(path);
}

static int boot(const Options *o,const char *cart_path,const char *base,const char *user)
{
    FILE *cart=fopen(cart_path,"rb");
    if (!cart || fseek(cart,0,SEEK_END)!=0) {
        if (cart) fclose(cart);
        fprintf(stderr,"boot-input: cannot read the cart image %s\n",cart_path);
        return EXIT_RUNTIME;
    }
    long bytes=ftell(cart);
    fclose(cart);
    if (bytes<=0 || (unsigned long)bytes!=n64c_boot_rom_bytes) {
        fprintf(stderr,"boot-input: cart image extent differs from the project layout\n");
        return EXIT_RUNTIME;
    }
    char shaders[INST_PATH],settings[INST_PATH],controls[INST_PATH];
    if (snprintf(shaders,sizeof shaders,"%sfast3d-shaders.o2r",base)>=(int)sizeof shaders ||
        snprintf(settings,sizeof settings,"%s/config/fast3d-settings.json",user)>=(int)sizeof settings ||
        snprintf(controls,sizeof controls,"%s/config/controls.cfg",user)>=(int)sizeof controls) {
        fprintf(stderr,"boot-input: path too long\n");
        return EXIT_RUNTIME;
    }
    if (access(shaders,R_OK)) {
        fprintf(stderr,"%s: the renderer's shader archive is missing (%s); reinstall the game\n",inst_game.game,
                shaders);
        return EXIT_PROGRAM_TREE;
    }
    char cpak[INST_PATH];
    const char *cpak_path=NULL;
    if (!o->cpak && !o->capture) { /* a capture run is a measurement: no card unless asked */
        if (snprintf(cpak,sizeof cpak,"%s/controller-pak.mpk",user)>=(int)sizeof cpak) {
            fprintf(stderr,"boot-input: path too long\n");
            return EXIT_RUNTIME;
        }
        cpak_path=cpak;
    } else if (o->cpak && strcmp(o->cpak,"off")) cpak_path=o->cpak;
    N64cConfig config={.rom_path=cart_path,.pak_path=cpak_path};
    n64c_init(&config);
    const size_t descriptor_counts[]={n64c_boot_load_count,n64c_boot_resource_count,
                                      n64c_boot_packed_count,n64c_boot_rom_image_count};
    size_t descriptors=0;
    for (size_t i=0;i<sizeof descriptor_counts/sizeof *descriptor_counts;i++) {
        if (descriptor_counts[i]>SIZE_MAX-descriptors) {
            fprintf(stderr,"boot-input: ROM descriptor count overflow\n");
            n64c_shutdown();
            return EXIT_RUNTIME;
        }
        descriptors+=descriptor_counts[i];
    }
    if (n64c_pi_reserve(descriptors)!=0) {
        fprintf(stderr,"boot-input: cannot allocate %zu ROM descriptors\n",descriptors);
        n64c_shutdown();
        return EXIT_RUNTIME;
    }
    for (size_t i=0;i<n64c_boot_load_count;i++) {
        const N64cBootLoad *load=&n64c_boot_loads[i];
        if (load->rom>(u32)bytes || load->bytes>(u32)bytes-load->rom ||
            n64c_pi_register(load->rom,load->rom+load->bytes,load->kind,load->fields)!=0) {
            fprintf(stderr,"boot-input: invalid ROM layout %s\n",load->symbol);
            n64c_shutdown();
            return EXIT_RUNTIME;
        }
    }
    for (size_t i=0;i<n64c_boot_resource_count;i++) {
        const N64cBootLoad *resource=&n64c_boot_resources[i];
        if (resource->rom>(u32)bytes || resource->bytes>(u32)bytes-resource->rom ||
            n64c_pi_register(resource->rom,resource->rom+resource->bytes,resource->kind,resource->fields)!=0) {
            fprintf(stderr,"boot-input: invalid ROM resource %s\n",resource->symbol);
            n64c_shutdown();
            return EXIT_RUNTIME;
        }
    }
    for (size_t i=0;i<n64c_boot_packed_count;i++) {
        const N64cBootLoad *stream=&n64c_boot_packed[i];
        if (stream->rom>(u32)bytes || stream->bytes>(u32)bytes-stream->rom ||
            n64c_pi_register_packed(stream->rom,stream->rom+stream->bytes,stream->kind,stream->fields)!=0) {
            fprintf(stderr,"boot-input: invalid RNC stream %s\n",stream->symbol);
            n64c_shutdown();
            return EXIT_RUNTIME;
        }
    }
    for (size_t i=0;i<n64c_boot_rom_image_count;i++) {
        const N64cBootRomImage *part=&n64c_boot_rom_image[i];
        if (part->rom>(u32)bytes || part->bytes>(u32)bytes-part->rom ||
            n64c_pi_register_image(part->rom,part->rom+part->bytes,n64c_boot_image_bytes+part->offset)!=0) {
            fprintf(stderr,"boot-input: invalid overlay data image at ROM 0x%08x\n",part->rom);
            n64c_shutdown();
            return EXIT_RUNTIME;
        }
    }
    u32 ram_bytes;
    n64c_parse_pak(o->pak,n64c_boot_ram_bytes,&ram_bytes);  /* parse() already refused any other text */
    N64cMemoryConfig memory={ram_bytes,(u32)bytes,n64c_boot_video_clock};
    if (!n64c_memory_open(&memory)) {
        fprintf(stderr,"boot-input: native address windows unavailable\n");
        n64c_shutdown();
        return EXIT_RUNTIME;
    }
    N64C_PROFILE_START();
    for (size_t i=0;i<n64c_boot_image_count;i++) {
        const N64cBootImage *part=&n64c_boot_image[i];
        void *destination=n64c_memory_ram(part->ram,part->bytes);
        if (!destination) {
            fprintf(stderr,"boot-input: data image outside RDRAM at 0x%08x\n",part->ram);
            n64c_shutdown();
            return EXIT_RUNTIME;
        }
        memcpy(destination,n64c_boot_image_bytes+part->offset,part->bytes);
    }
    for (size_t i=0;i<n64c_boot_load_count;i++) {
        const N64cBootLoad *load=&n64c_boot_loads[i];
        void *destination=n64c_memory_ram(load->ram,load->bytes);
        if (!destination || n64c_pi_read(load->rom,destination,load->bytes)!=0) {
            fprintf(stderr,"boot-input: cannot load %s\n",load->symbol);
            n64c_shutdown();
            return EXIT_RUNTIME;
        }
    }
    if (n64c_boot_system.initialized &&
        !n64c_system_bind(&n64c_boot_system,n64c_boot_tv_type,n64c_boot_reset_type,ram_bytes)) {
        fprintf(stderr,"boot-input: invalid system globals\n");
        n64c_shutdown();
        return EXIT_RUNTIME;
    }
    const char *replay=getenv("N64C_REPLAY");
    if (replay && *replay) {
        if (!N64C_LINKED(n64c_replay_run)) {
            fprintf(stderr,"boot-input: this build has no function replay (N64C_REPLAY)\n");
            n64c_shutdown();
            return EXIT_RUNTIME;
        }
        int rc=n64c_replay_run(replay);
        n64c_shutdown();
        return rc;
    }
    const int live=!o->capture;
    n64c_input_set_config_path(controls); /* the Controls page keeps its binds here; the window loads them */
    n64c_boot_frame_host(shaders,settings,o->offscreen);
    {
        const char *env=getenv("N64C_FPS_UNLOCK");
        n64c_boot_frame_unlock(o->fps_unlock || (env && *env && strcmp(env,"0")));
    }
    if (!n64c_boot_frame_start(o->fb_w,o->fb_h,ram_bytes,n64c_boot_audio_abi,
                               live ? o->out_w : 0,live ? o->out_h : 0)) {
        fprintf(stderr,"boot-input: cannot configure native frame capture\n");
        n64c_shutdown();
        return EXIT_RUNTIME;
    }
    n64c_game_scheduler_attach();
    if (n64c_trace_init) n64c_trace_init();
    if (n64c_runon_init) n64c_runon_init();
    fprintf(stderr,"boot-entry: original C\n");
    if (live) fprintf(stderr,"boot-step: original C\n");
    fflush(stderr);
    int result=n64c_boot_entry();
    int ok=!result && (live ? n64c_boot_frame_live() : n64c_boot_frame_capture(o->capture,o->retrace,o->out_w,o->out_h));
    const int rc=n64c_runon_end ? n64c_runon_end(ok ? 0 : 1) : ok ? 0 : 1;
    n64c_boot_frame_close();
    n64c_shutdown();
    return rc;
}

static void version(void)
{
    printf("%s\n",inst_game.version);
    for (size_t i=0;i<inst_game.dump_count;i++)
        if (inst_game.dumps[i].supported) printf("supports %s %s\n",inst_game.dumps[i].id,inst_game.dumps[i].label);
}

/* --rom PATH (DESIGN 3.3): the same dump as the assets is the fast path; another supported dump is extracted. */
static int from_rom(const Options *o,const char *user,const char *base,InstAssets *assets)
{
    InstRom rom;
    if (inst_rom_load(&rom,&inst_game,o->rom)!=INST_ROM_SUPPORTED) {
        inst_ui_message(!o->no_dialog,true,"Can't use this ROM",rom.message);
        inst_rom_free(&rom);
        return INST_EXIT_USER;
    }
    if (assets->status==INST_ASSETS_OK && !assets->predistributed && !strcmp(assets->dump->sha256,rom.sha256)) {
        inst_rom_free(&rom);
        return 0;
    }
    char err[512]="";
    int extracted=inst_ui_extract(!o->no_dialog,&inst_game,user,&rom,err,sizeof err);
    inst_rom_free(&rom);
    if (extracted) {
        fprintf(stderr,"%s: %s\n",inst_game.game,extracted>0 ? "extraction cancelled" : err);
        return extracted>0 ? INST_EXIT_USER : EXIT_RUNTIME;
    }
    if (inst_assets_check(assets,&inst_game,user,base)!=INST_ASSETS_OK) {
        fprintf(stderr,"%s: the extracted game data does not verify: %s\n",inst_game.game,assets->message);
        return EXIT_RUNTIME;
    }
    return 0;
}

static int program(int argc,char **argv)
{
    Options o;
    if (!parse(argc,argv,&o)) return EXIT_USAGE;
    if (o.help) { usage(stdout,argv[0]);return 0; }
    if (o.version) { version();return 0; }
    const char *base=SDL_GetBasePath();
    if (!base) {
        fprintf(stderr,"%s: cannot find the program folder\n",inst_game.game);
        return EXIT_PROGRAM_TREE;
    }
    char user[INST_PATH];
    if (inst_user_dir(user,sizeof user,o.user_dir,base,NULL)) {
        char *pref=SDL_GetPrefPath("",inst_game.package);
        int bad=!pref || inst_user_dir(user,sizeof user,NULL,NULL,pref);
        SDL_free(pref);
        if (bad) {
            fprintf(stderr,"%s: no user folder: %s\n",inst_game.game,SDL_GetError());
            return EXIT_RUNTIME;
        }
    }
    if (inst_user_prepare(user)) {
        fprintf(stderr,"%s: cannot create the user folder %s: %s\n",inst_game.game,user,strerror(errno));
        return EXIT_RUNTIME;
    }
    tee_log(user,inst_game.game);
    InstAssets assets;
    InstAssetsStatus status=inst_assets_check(&assets,&inst_game,user,base);
    if (assets.repaired) fprintf(stderr,"assets: %s\n",assets.message);
    if (status==INST_ASSETS_IO) {
        fprintf(stderr,"%s: %s/assets: %s\n",inst_game.game,user,assets.message);
        return EXIT_RUNTIME;
    }
    if (o.verify_assets) {
        if (status==INST_ASSETS_OK) {
            fprintf(stderr,"assets: ok %s %s\n",assets.dump->id,assets.dir);
            return 0;
        }
        fprintf(stderr,"assets: %s: %s\n",status==INST_ASSETS_MISSING ? "missing" :
                status==INST_ASSETS_NEWER ? "made by a newer version" : "Your extracted game data is damaged",
                assets.message);
        return INST_EXIT_USER;
    }
    if (o.rom && status!=INST_ASSETS_NEWER) {
        int rc=from_rom(&o,user,base,&assets);
        if (rc) return rc;
        status=assets.status;
    }
    if (status!=INST_ASSETS_OK) {
        int rc=inst_ui_first_run(!o.no_dialog,&inst_game,user,base,&assets);
        if (rc) return rc;
    }
    if (o.extract_only) {
        fprintf(stderr,"assets: ok %s %s\n",assets.dump->id,assets.dir);
        return 0;
    }
    return boot(&o,assets.cart,base,user);
}

int main(int argc,char **argv)
{
    const int elsewhere=n64c_host_process_start();
    if (elsewhere>=0) return elsewhere;
    return n64c_host_run(program,argc,argv);
}
