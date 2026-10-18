/* Forward an explicitly named virtual HID device family into one Wayland seat.
 * Headless wlroots has no libinput backend. Never enumerate physical devices.
 * Run with permission to read/grab the configured devices; gamepads remain SDL devices.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <linux/netlink.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/sysmacros.h>
#include <unistd.h>
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>
#include "virtual-keyboard.h"
#include "virtual-pointer.h"

static struct wl_display *display;
static struct wl_seat *seat;
static struct zwp_virtual_keyboard_manager_v1 *km;
static struct zwlr_virtual_pointer_manager_v1 *pm;
static struct zwp_virtual_keyboard_v1 *keyboard;
static struct zwlr_virtual_pointer_v1 *pointer;
static struct xkb_state *state;
static const char *marker;
static gid_t input_gid;
struct device { int fd, number, keyboard, absolute, x, y, max_x, max_y; char properties[1024]; size_t properties_len; };
static struct device devices[64];
static size_t count;
static volatile sig_atomic_t running=1;
static void stop(int signal_number) { (void)signal_number; running=0; }

static void die(const char *s) { perror(s); exit(1); }
static void global(void *data, struct wl_registry *r, uint32_t name, const char *iface, uint32_t version) {
    (void)data; (void)version;
    if (!strcmp(iface, "wl_seat") && !seat) seat = wl_registry_bind(r, name, &wl_seat_interface, 1);
    if (!strcmp(iface, "zwp_virtual_keyboard_manager_v1")) km = wl_registry_bind(r, name, &zwp_virtual_keyboard_manager_v1_interface, 1);
    if (!strcmp(iface, "zwlr_virtual_pointer_manager_v1")) pm = wl_registry_bind(r, name, &zwlr_virtual_pointer_manager_v1_interface, 1);
}
static void removed(void *d, struct wl_registry *r, uint32_t n) { (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {global, removed};
/* libudev monitor wire format: namespace-local notification.
 * A container can see devtmpfs devices but miss the host netlink hotplug event.
 * This relay names only our virtual controller; it never triggers host sysfs.
 */
static uint32_t hash32(const char *text) {
    size_t len=strlen(text), i=0;
    uint32_t h=(uint32_t)len, m=0x5bd1e995;
    while(len-i>=4) {
        uint32_t k; memcpy(&k,text+i,4); k*=m; k^=k>>24; k*=m; h=h*m^k; i+=4;
    }
    size_t tail=len-i;
    for(size_t j=0;j<tail;j++) h^=(uint32_t)(unsigned char)text[i+j]<<(j*8);
    if(tail) h*=m;
    h^=h>>13; h*=m; h^=h>>15; return h;
}
static void notify_controller(struct device *d, const char *action) {
    struct {
        char prefix[8]; uint32_t magic, size, offset, length, subsystem, devtype, bloom_hi, bloom_lo;
    } header={.prefix="libudev", .magic=htonl(0xfeedcafe), .size=40, .offset=40,
              .subsystem=htonl(hash32("input")), .bloom_hi=UINT32_MAX, .bloom_lo=UINT32_MAX};
    char body[1200], packet[1240];
    int n=snprintf(body,sizeof(body),"ACTION=%s",action)+1;
    memcpy(body+n,d->properties,d->properties_len); n+=(int)d->properties_len;
    header.length=(uint32_t)n;
    memcpy(packet,&header,sizeof(header)); memcpy(packet+sizeof(header),body,n);
    int fd=socket(AF_NETLINK,SOCK_RAW|SOCK_CLOEXEC,NETLINK_KOBJECT_UEVENT);
    if(fd<0) die("controller netlink");
    struct sockaddr_nl addr={.nl_family=AF_NETLINK,.nl_groups=2};
    if(sendto(fd,packet,sizeof(header)+n,0,(struct sockaddr *)&addr,sizeof(addr))<0 && errno!=ECONNREFUSED)
        die("controller hotplug relay");
    close(fd);
    fprintf(stderr,"controller %s event%d relayed in local namespace\n",action,d->number);
}
static int controller_properties(struct device *d) {
    unsigned char keys[(KEY_MAX+8)/8]={0};
    if(ioctl(d->fd,EVIOCGBIT(EV_KEY,sizeof(keys)),keys)<0 || !(keys[BTN_GAMEPAD/8]&(1<<(BTN_GAMEPAD%8)))) return 0;
    char path[128], resolved[512];
    snprintf(path,sizeof(path),"/sys/class/input/event%d",d->number);
    if(!realpath(path,resolved)) die("controller sysfs path");
    struct stat st; if(fstat(d->fd,&st)<0) die("controller stat");
    int len=snprintf(d->properties,sizeof(d->properties),
        "DEVPATH=%s%cDEVNAME=/dev/input/event%d%cSUBSYSTEM=input%cID_INPUT=1%cID_INPUT_JOYSTICK=1%cMAJOR=%u%cMINOR=%u%cSEQNUM=1",
        resolved+4,0,d->number,0,0,0,0,major(st.st_rdev),0,minor(st.st_rdev),0);
    if(len<0 || (size_t)len>=sizeof(d->properties)) die("controller properties");
    d->properties_len=(size_t)len+1; return 1;
}
static void scan(void) {
    DIR *dir = opendir("/sys/class/input");
    if (!dir) die("input directory");
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        int number;
        if (sscanf(entry->d_name, "event%d", &number) != 1) continue;
        int seen = 0;
        for (size_t i=0; i<count; i++) if (devices[i].number == number) seen = 1;
        char path[512], name[256];
        snprintf(path, sizeof(path), "/sys/class/input/event%d/device/name", number);
        FILE *f = fopen(path, "r");
        if (!f) continue;
        char *ok = fgets(name, sizeof(name), f); fclose(f);
        if (!ok || !strstr(name, marker)) continue;
        name[strcspn(name, "\n")] = 0;
        snprintf(path, sizeof(path), "/dev/input/event%d", number);
        int fd = open(path, O_RDONLY|O_NONBLOCK|O_CLOEXEC);
        if (fd < 0) { if (errno == ENOENT) continue; die(path); }
        /* Only this named virtual family gets access for the app's input group. */
        struct stat st;
        if (fstat(fd, &st) < 0) die("input stat");
        if ((st.st_gid != input_gid || (st.st_mode & 0777) != 0660) &&
            (fchown(fd, -1, input_gid) < 0 || fchmod(fd, 0660) < 0)) die("input permissions");
        if (seen) { close(fd); continue; }
        int is_key = strstr(name, "Keyboard") != NULL;
        int is_mouse = strstr(name, "Mouse") != NULL;
        if (count == 64) die("too many virtual devices");
        struct device d = {.fd=fd, .number=number, .keyboard=is_key, .absolute=strstr(name,"Absolute") != NULL};
        if (!is_key && !is_mouse) {
            if (!controller_properties(&d)) { close(fd); continue; }
            d.keyboard=-1;
            notify_controller(&d,"add");
        } else if (ioctl(fd, EVIOCGRAB, 1) < 0) die("exclusive virtual input grab");
        if (d.absolute) {
            struct input_absinfo x, y;
            if (ioctl(fd, EVIOCGABS(ABS_X), &x)<0 || ioctl(fd, EVIOCGABS(ABS_Y), &y)<0) die("absolute bounds");
            d.max_x=x.maximum; d.max_y=y.maximum;
        }
        devices[count++] = d;
        fprintf(stderr, "attached event%d %s\n", number, name);
    }
    closedir(dir);
}
static void event(struct device *d, struct input_event *e) {
    if(d->keyboard<0) return;
    uint32_t time = (uint32_t)(e->time.tv_sec*1000 + e->time.tv_usec/1000);
    if (d->keyboard && e->type == EV_KEY && e->value != 2) {
        zwp_virtual_keyboard_v1_key(keyboard, time, e->code, e->value);
        xkb_state_update_key(state, e->code+8, e->value ? XKB_KEY_DOWN : XKB_KEY_UP);
        zwp_virtual_keyboard_v1_modifiers(keyboard,
            xkb_state_serialize_mods(state, XKB_STATE_MODS_DEPRESSED),
            xkb_state_serialize_mods(state, XKB_STATE_MODS_LATCHED),
            xkb_state_serialize_mods(state, XKB_STATE_MODS_LOCKED),
            xkb_state_serialize_layout(state, XKB_STATE_LAYOUT_EFFECTIVE));
    } else if (!d->keyboard) {
        if (e->type == EV_KEY && e->code >= BTN_MOUSE && e->code <= BTN_TASK)
            zwlr_virtual_pointer_v1_button(pointer,time,e->code,e->value);
        if (e->type == EV_REL) {
            if (e->code == REL_X || e->code == REL_Y)
                zwlr_virtual_pointer_v1_motion(pointer,time,wl_fixed_from_int(e->code==REL_X?e->value:0),wl_fixed_from_int(e->code==REL_Y?e->value:0));
            if (e->code == REL_WHEEL || e->code == REL_HWHEEL)
                zwlr_virtual_pointer_v1_axis(pointer,time,e->code==REL_WHEEL?0:1,wl_fixed_from_int(-e->value*15));
        }
        if (e->type == EV_ABS) {
            if (e->code == ABS_X) d->x=e->value;
            if (e->code == ABS_Y) d->y=e->value;
        }
        if (e->type == EV_SYN && e->code == SYN_REPORT) {
            if (d->absolute) zwlr_virtual_pointer_v1_motion_absolute(pointer,time,d->x,d->y,d->max_x,d->max_y);
            zwlr_virtual_pointer_v1_frame(pointer);
        }
    }
}
int main(int argc, char **argv) {
    if (argc != 3 || !argv[1][0]) { fprintf(stderr,"usage: input-bridge DEVICE_NAME_MARKER INPUT_GID\n"); return 2; }
    marker=argv[1]; input_gid=(gid_t)strtoul(argv[2],NULL,10);
    display=wl_display_connect(NULL); if(!display) die("Wayland connect");
    struct wl_registry *registry=wl_display_get_registry(display);
    wl_registry_add_listener(registry,&registry_listener,NULL);
    if(wl_display_roundtrip(display)<0 || !seat || !km || !pm) die("Wayland input protocols");
    keyboard=zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(km,seat);
    pointer=zwlr_virtual_pointer_manager_v1_create_virtual_pointer(pm,seat);
    struct xkb_context *ctx=xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    struct xkb_keymap *map=xkb_keymap_new_from_names(ctx,NULL,XKB_KEYMAP_COMPILE_NO_FLAGS);
    if(!map) die("keymap");
    state=xkb_state_new(map);
    char *text=xkb_keymap_get_as_string(map,XKB_KEYMAP_FORMAT_TEXT_V1);
    size_t len=strlen(text)+1;
    int mem=memfd_create("stream-keymap",MFD_CLOEXEC);
    if(mem<0 || write(mem,text,len)!=(ssize_t)len) die("keymap fd");
    zwp_virtual_keyboard_v1_keymap(keyboard,WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1,mem,len);
    close(mem); free(text);
    int notify=inotify_init1(IN_NONBLOCK|IN_CLOEXEC);
    if(notify<0 || inotify_add_watch(notify,"/dev/input",IN_CREATE|IN_MOVED_TO|IN_ATTRIB)<0) die("input watch");
    signal(SIGTERM,stop); signal(SIGINT,stop);
    scan();
    while (running) {
        if(wl_display_dispatch_pending(display)<0 || wl_display_flush(display)<0) die("Wayland dispatch");
        struct pollfd fds[66]={{.fd=wl_display_get_fd(display),.events=POLLIN},{.fd=notify,.events=POLLIN}};
        size_t n=count;
        for(size_t i=0;i<n;i++) fds[i+2]=(struct pollfd){.fd=devices[i].fd,.events=POLLIN};
        if(poll(fds,n+2,-1)<0) { if(errno==EINTR) continue; die("poll"); }
        if(fds[0].revents && wl_display_dispatch(display)<0) die("Wayland disconnected");
        /* Process backwards so removal doesn't invalidate another ready descriptor. */
        for(size_t i=n;i-->0;) {
            if(fds[i+2].revents & (POLLHUP|POLLERR)) {
                if(devices[i].keyboard<0) notify_controller(&devices[i],"remove");
                close(devices[i].fd); devices[i]=devices[--count]; continue;
            }
            if(fds[i+2].revents & POLLIN) {
                struct input_event e;
                while(read(devices[i].fd,&e,sizeof(e))==sizeof(e)) event(&devices[i],&e);
            }
        }
        if(fds[1].revents) { char buf[4096]; while(read(notify,buf,sizeof(buf))>0) {} scan(); }
    }
    for(size_t i=0;i<count;i++) {
        if(devices[i].keyboard<0) notify_controller(&devices[i],"remove");
        close(devices[i].fd);
    }
    zwp_virtual_keyboard_v1_destroy(keyboard);
    zwlr_virtual_pointer_v1_destroy(pointer);
    wl_display_flush(display);
    wl_display_disconnect(display);
    return 0;
}
