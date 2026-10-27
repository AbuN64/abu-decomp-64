/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "n64c_touch.h"
#include "n64c_input.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define STICK_MAX 80
#define DEAD 0.15f
#define R_MIN 0.02f
#define R_MAX 0.25f
typedef struct { long long id; float x, y; int zone; float grab_dx, grab_dy; int active; } Finger;
static N64cTouchLayout layout;
static int layout_ready, editing, seen;
static float aspect = 16.0f / 9.0f;
static Finger fingers[N64C_TOUCH_FINGERS];

static const char *const zone_names[N64C_TOUCH_ZONES] = {"a", "b", "z", "l", "r", "start", "c_up", "c_down", "c_left",
    "c_right", "d_up", "d_down", "d_left", "d_right", "stick"};
const char *n64c_touch_zone_name(int zone) { return zone >= 0 && zone < N64C_TOUCH_ZONES ? zone_names[zone] : "?"; }
void n64c_touch_defaults(N64cTouchLayout *l)
{
    static const N64cTouchZone d[N64C_TOUCH_ZONES] = {
        [N64C_ACT_A] = {0.90f, 0.80f, 0.07f}, [N64C_ACT_B] = {0.80f, 0.88f, 0.06f}, [N64C_ACT_Z] = {0.08f, 0.45f, 0.06f},
        [N64C_ACT_L] = {0.08f, 0.10f, 0.05f}, [N64C_ACT_R] = {0.92f, 0.10f, 0.05f}, [N64C_ACT_START] = {0.50f, 0.92f, 0.04f},
        [N64C_ACT_CUP] = {0.78f, 0.50f, 0.04f}, [N64C_ACT_CDOWN] = {0.78f, 0.66f, 0.04f},
        [N64C_ACT_CLEFT] = {0.70f, 0.58f, 0.04f}, [N64C_ACT_CRIGHT] = {0.86f, 0.58f, 0.04f},
        [N64C_ACT_DUP] = {0.36f, 0.80f, 0.03f}, [N64C_ACT_DDOWN] = {0.36f, 0.92f, 0.03f},
        [N64C_ACT_DLEFT] = {0.30f, 0.86f, 0.03f}, [N64C_ACT_DRIGHT] = {0.42f, 0.86f, 0.03f},
        [N64C_TOUCH_STICK] = {0.16f, 0.72f, 0.12f}};
    memcpy(l->zone, d, sizeof d);
}
static float clampf(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }
size_t n64c_touch_format(const N64cTouchLayout *l, char *buf, size_t cap)
{
    size_t n = 0;
    char line[96];
    for (int i = 0; i < N64C_TOUCH_ZONES; ++i) {
        int len = snprintf(line, sizeof line, "%s = %.4f %.4f %.4f\n", zone_names[i], l->zone[i].x, l->zone[i].y, l->zone[i].r);
        if (buf && n + (size_t)len < cap) memcpy(buf + n, line, (size_t)len + 1);
        n += (size_t)len;
    }
    return n;
}
static void fail(char *err, size_t cap, int line, const char *why)
{
    if (err && cap) snprintf(err, cap, "line %d: %s", line, why);
}
int n64c_touch_parse(N64cTouchLayout *l, const char *text, char *err, size_t errcap)
{
    N64cTouchLayout work = *l;
    int line_no = 0;
    const char *p = text;
    while (*p) {
        char line[128];
        size_t len = strcspn(p, "\n");
        ++line_no;
        if (len >= sizeof line) { fail(err, errcap, line_no, "line too long"); return 0; }
        memcpy(line, p, len);
        line[len] = 0;
        p += len + (p[len] == '\n');
        char *s = line;
        while (*s == ' ' || *s == '\t' || *s == '\r') ++s;
        if (!*s || *s == '#') continue;
        char name[16], extra;
        float x, y, r;
        if (sscanf(s, "%15s = %f %f %f %c", name, &x, &y, &r, &extra) != 4) { fail(err, errcap, line_no, "expected: name = x y radius"); return 0; }
        int zone = -1;
        for (int i = 0; i < N64C_TOUCH_ZONES; ++i)
            if (!strcmp(name, zone_names[i])) zone = i;
        if (zone < 0) { fail(err, errcap, line_no, "unknown zone"); return 0; }
        if (!(x >= 0 && x <= 1 && y >= 0 && y <= 1 && r >= R_MIN && r <= R_MAX)) { fail(err, errcap, line_no, "value out of range"); return 0; }
        work.zone[zone] = (N64cTouchZone){x, y, r};
    }
    *l = work;
    return 1;
}
int n64c_touch_save(const N64cTouchLayout *l, const char *path)
{
    size_t need = n64c_touch_format(l, NULL, 0);
    char *buf = malloc(need + 1), tmp[600];
    if (!buf) return 0;
    n64c_touch_format(l, buf, need + 1);
    int ok = snprintf(tmp, sizeof tmp, "%s.tmp", path) < (int)sizeof tmp;
    FILE *f = ok ? fopen(tmp, "wb") : NULL;
    ok = f && fwrite(buf, 1, need, f) == need;
    if (f) ok = fclose(f) == 0 && ok;
    ok = ok && rename(tmp, path) == 0;
    if (!ok && f) remove(tmp);
    free(buf);
    return ok;
}
int n64c_touch_load(N64cTouchLayout *l, const char *path, char *err, size_t errcap)
{
    FILE *f = fopen(path, "rb");
    if (!f) return 1;
    char *buf = malloc(8192);
    size_t n = buf ? fread(buf, 1, 8191, f) : 0;
    fclose(f);
    if (!buf) return 0;
    buf[n] = 0;
    int ok = n64c_touch_parse(l, buf, err, errcap);
    free(buf);
    return ok;
}
int n64c_touch_corner(float x, float y)
{
    if (y < 0.08f && x < 0.06f) return N64C_TOUCH_CORNER_MENU;
    if (y < 0.08f && x > 0.94f) return N64C_TOUCH_CORNER_RESET;
    return N64C_TOUCH_CORNER_NONE;
}
N64cTouchLayout *n64c_touch_layout(void)
{
    if (!layout_ready) { n64c_touch_defaults(&layout); layout_ready = 1; }
    return &layout;
}
void n64c_touch_set_aspect(float a) { if (a > 0.1f && a < 10.0f) aspect = a; }
int n64c_touch_seen(void) { return seen; }
int n64c_touch_editing(void) { return editing; }
void n64c_touch_release_all(void) { memset(fingers, 0, sizeof fingers); }
void n64c_touch_set_editing(int e) { editing = e != 0; n64c_touch_release_all(); }

/* Distance in window widths: y is scaled by 1/aspect so a zone is a circle on screen. */
static float dist(const N64cTouchZone *z, float x, float y)
{
    float dx = x - z->x, dy = (y - z->y) / aspect;
    return sqrtf(dx * dx + dy * dy);
}
static int hit(float x, float y)
{
    N64cTouchLayout *l = n64c_touch_layout();
    int best = -1;
    float best_d = 1e9f;
    for (int i = 0; i < N64C_TOUCH_ZONES; ++i) {
        float d = dist(&l->zone[i], x, y);
        if (d <= l->zone[i].r && d < best_d) { best = i; best_d = d; }
    }
    return best;
}
static Finger *find(long long id)
{
    for (int i = 0; i < N64C_TOUCH_FINGERS; ++i) if (fingers[i].active && fingers[i].id == id) return &fingers[i];
    return NULL;
}
int n64c_touch_down(long long id, float x, float y)
{
    seen = 1;
    if (find(id)) return 0;
    int corner = n64c_touch_corner(x, y);
    if (corner) return corner;
    for (int i = 0; i < N64C_TOUCH_FINGERS; ++i) {
        if (fingers[i].active) continue;
        Finger *f = &fingers[i];
        *f = (Finger){id, x, y, hit(x, y), 0, 0, 1};
        if (editing && f->zone >= 0) {
            f->grab_dx = n64c_touch_layout()->zone[f->zone].x - x;
            f->grab_dy = n64c_touch_layout()->zone[f->zone].y - y;
        } else if (editing) {
            f->zone = -2; /* a second finger that scales the zone held by another finger */
            for (int j = 0; j < N64C_TOUCH_FINGERS; ++j)
                if (j != i && fingers[j].active && fingers[j].zone >= 0) f->zone = -100 - fingers[j].zone;
        }
        return 0;
    }
    return 0;
}
void n64c_touch_move(long long id, float x, float y)
{
    Finger *f = find(id);
    if (!f) return;
    N64cTouchLayout *l = n64c_touch_layout();
    if (editing && f->zone >= 0) {
        l->zone[f->zone].x = clampf(x + f->grab_dx, 0.0f, 1.0f);
        l->zone[f->zone].y = clampf(y + f->grab_dy, 0.0f, 1.0f);
    } else if (editing && f->zone <= -100) {
        int z = -100 - f->zone;
        l->zone[z].r = clampf(l->zone[z].r + (f->y - y) * 0.25f, R_MIN, R_MAX); /* drag up grows, down shrinks */
    }
    f->x = x;
    f->y = y;
}
void n64c_touch_up(long long id)
{
    Finger *f = find(id);
    if (f) f->active = 0;
}
void n64c_touch_pad(unsigned *buttons, int *stick_x, int *stick_y)
{
    N64cTouchLayout *l = n64c_touch_layout();
    *buttons = 0;
    *stick_x = *stick_y = 0;
    if (editing) return;
    for (int i = 0; i < N64C_TOUCH_FINGERS; ++i) {
        const Finger *f = &fingers[i];
        if (!f->active) continue;
        const N64cTouchZone *s = &l->zone[N64C_TOUCH_STICK];
        if (f->zone == N64C_TOUCH_STICK) { /* a drag that began on the stick keeps steering outside it */
            float dx = (f->x - s->x) / s->r, dy = (f->y - s->y) / (s->r * aspect);
            float len = sqrtf(dx * dx + dy * dy);
            if (len > 1.0f) { dx /= len; dy /= len; len = 1.0f; }
            if (len >= DEAD) {
                *stick_x = (int)(dx * STICK_MAX + (dx < 0 ? -0.5f : 0.5f));
                *stick_y = (int)(-dy * STICK_MAX + (dy > 0 ? -0.5f : 0.5f));
            }
            continue;
        }
        for (int z = 0; z < N64C_TOUCH_STICK; ++z) /* sliding onto a neighbouring button presses it too */
            if (dist(&l->zone[z], f->x, f->y) <= l->zone[z].r) *buttons |= n64c_action_button(z);
    }
}
unsigned n64c_touch_active_zones(void)
{
    unsigned mask = 0;
    N64cTouchLayout *l = n64c_touch_layout();
    for (int i = 0; i < N64C_TOUCH_FINGERS; ++i) {
        const Finger *f = &fingers[i];
        if (!f->active) continue;
        if (f->zone >= 0 && f->zone <= N64C_TOUCH_STICK && (editing || f->zone == N64C_TOUCH_STICK)) mask |= 1u << f->zone;
        else if (f->zone <= -100) mask |= 1u << (-100 - f->zone);
        for (int z = 0; !editing && z < N64C_TOUCH_STICK; ++z)
            if (dist(&l->zone[z], f->x, f->y) <= l->zone[z].r) mask |= 1u << z;
    }
    return mask;
}
