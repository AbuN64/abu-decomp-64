/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "n64c_binds.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *const action_names[N64C_ACT_COUNT] = {
    "A", "B", "Z", "L", "R", "START", "C-UP", "C-DOWN", "C-LEFT", "C-RIGHT",
    "D-UP", "D-DOWN", "D-LEFT", "D-RIGHT", "STICK-UP", "STICK-DOWN", "STICK-LEFT", "STICK-RIGHT"};
const char *n64c_action_name(int action)
{
    return action >= 0 && action < N64C_ACT_COUNT ? action_names[action] : "?";
}
int n64c_bind_fits(int device, const N64cBind *bind)
{
    switch (bind->kind) {
    case N64C_BIND_NONE: return 1;
    case N64C_BIND_KEY: return device == N64C_DEV_KEYBOARD && bind->code > 0;
    case N64C_BIND_PAD_BUTTON: return device == N64C_DEV_GAMEPAD && bind->code >= 0;
    case N64C_BIND_PAD_AXIS: return device == N64C_DEV_GAMEPAD && bind->code >= 0 && (bind->dir == 1 || bind->dir == -1);
    case N64C_BIND_MOUSE_BUTTON: return device == N64C_DEV_MOUSE && bind->code > 0;
    case N64C_BIND_MOUSE_LOOK:
        return device == N64C_DEV_MOUSE && (bind->code == 0 || bind->code == 1) && (bind->dir == 1 || bind->dir == -1);
    }
    return 0;
}
int n64c_bind_equal(const N64cBind *a, const N64cBind *b)
{
    return a->kind == b->kind && a->code == b->code && a->dir == b->dir;
}
int n64c_binds_set(N64cBinds *binds, int player, int action, int slot, N64cBind bind)
{
    if (!binds || player < 0 || player >= N64C_PLAYERS || action < 0 || action >= N64C_ACT_COUNT || slot < 0 ||
        slot >= N64C_SLOT_COUNT || !n64c_bind_fits(n64c_bind_slot_device(slot), &bind))
        return 0;
    if (bind.kind == N64C_BIND_NONE) bind = (N64cBind){0};
    binds->player[player].slot[action][slot] = bind;
    return 1;
}
/* Keyboard and mouse are one physical device for every port; a pad belongs to its port only. */
int n64c_binds_conflict(const N64cBinds *binds, int player, int action, int slot, int *other_player,
                        int *other_action)
{
    if (!binds || player < 0 || player >= N64C_PLAYERS || action < 0 || action >= N64C_ACT_COUNT || slot < 0 ||
        slot >= N64C_SLOT_COUNT)
        return 0;
    const N64cBind *mine = &binds->player[player].slot[action][slot];
    if (mine->kind == N64C_BIND_NONE) return 0;
    int device = n64c_bind_slot_device(slot);
    for (int p = 0; p < N64C_PLAYERS; ++p) {
        if (device == N64C_DEV_GAMEPAD && p != player) continue;
        for (int a = 0; a < N64C_ACT_COUNT; ++a) {
            if (p == player && a == action) continue;
            for (int s = device * N64C_SLOTS_PER_DEVICE; s < (device + 1) * N64C_SLOTS_PER_DEVICE; ++s)
                if (n64c_bind_equal(mine, &binds->player[p].slot[a][s])) {
                    if (other_player) *other_player = p;
                    if (other_action) *other_action = a;
                    return 1;
                }
        }
    }
    return 0;
}
int n64c_binds_conflict_count(const N64cBinds *binds)
{
    int count = 0;
    for (int p = 0; p < N64C_PLAYERS; ++p)
        for (int a = 0; a < N64C_ACT_COUNT; ++a)
            for (int s = 0; s < N64C_SLOT_COUNT; ++s)
                count += n64c_binds_conflict(binds, p, a, s, NULL, NULL);
    return count;
}
int n64c_binds_player_has_device(const N64cBinds *binds, int player, int device)
{
    for (int a = 0; a < N64C_ACT_COUNT; ++a)
        for (int s = device * N64C_SLOTS_PER_DEVICE; s < (device + 1) * N64C_SLOTS_PER_DEVICE; ++s)
            if (binds->player[player].slot[a][s].kind != N64C_BIND_NONE) return 1;
    return 0;
}
int n64c_binds_has_mouse_look(const N64cBinds *binds)
{
    for (int p = 0; p < N64C_PLAYERS; ++p)
        for (int a = 0; a < N64C_ACT_COUNT; ++a)
            for (int s = 4; s < N64C_SLOT_COUNT; ++s)
                if (binds->player[p].slot[a][s].kind == N64C_BIND_MOUSE_LOOK) return 1;
    return 0;
}
/* ---- text form ---- */
static int format_bind(const N64cBind *b, char *out, size_t cap)
{
    switch (b->kind) {
    case N64C_BIND_KEY: return snprintf(out, cap, "key:%d", b->code);
    case N64C_BIND_PAD_BUTTON: return snprintf(out, cap, "btn:%d", b->code);
    case N64C_BIND_PAD_AXIS: return snprintf(out, cap, "axis:%d%c", b->code, b->dir > 0 ? '+' : '-');
    case N64C_BIND_MOUSE_BUTTON: return snprintf(out, cap, "mouse:%d", b->code);
    case N64C_BIND_MOUSE_LOOK: return snprintf(out, cap, "look:%c%c", b->code ? 'y' : 'x', b->dir > 0 ? '+' : '-');
    default: return snprintf(out, cap, "none");
    }
}
size_t n64c_binds_format(const N64cBinds *binds, char *buf, size_t cap)
{
    size_t n = 0;
    char line[256];
#define EMIT(...) do { int w_ = snprintf(line, sizeof line, __VA_ARGS__); \
    if (n + (size_t)w_ + 1 <= cap) { memcpy(buf + n, line, (size_t)w_ + 1); } \
    n += (size_t)w_; } while (0)
    EMIT("# rage-ports controls: one line per N64 input, slots keyboard1 keyboard2 pad1 pad2 mouse1 mouse2\n");
    EMIT("# key:<scancode> btn:<button> axis:<axis>+|- mouse:<button> look:x|y+|- none\n");
    EMIT("deadzone = %d\nbutton_threshold = %d\nmouse_sensitivity = %d\n", binds->deadzone, binds->button_threshold,
         binds->mouse_sensitivity);
    for (int p = 0; p < N64C_PLAYERS; ++p) {
        EMIT("[player%d]\n", p + 1);
        for (int a = 0; a < N64C_ACT_COUNT; ++a) {
            char row[200], tok[32];
            int len = snprintf(row, sizeof row, "%s =", n64c_action_name(a));
            for (int s = 0; s < N64C_SLOT_COUNT; ++s) {
                format_bind(&binds->player[p].slot[a][s], tok, sizeof tok);
                len += snprintf(row + len, sizeof row - (size_t)len, " %s", tok);
            }
            EMIT("%s\n", row);
        }
    }
#undef EMIT
    if (cap) buf[n < cap ? n : cap - 1] = 0;
    return n;
}
static int parse_int(const char *s, char **end) { return (int)strtol(s, end, 10); }
static int parse_bind(const char *tok, N64cBind *out)
{
    char *end;
    *out = (N64cBind){0};
    if (!strcmp(tok, "none")) return 1;
    if (!strncmp(tok, "key:", 4)) { out->kind = N64C_BIND_KEY; out->code = (short)parse_int(tok + 4, &end); return !*end; }
    if (!strncmp(tok, "btn:", 4)) { out->kind = N64C_BIND_PAD_BUTTON; out->code = (short)parse_int(tok + 4, &end); return !*end; }
    if (!strncmp(tok, "mouse:", 6)) { out->kind = N64C_BIND_MOUSE_BUTTON; out->code = (short)parse_int(tok + 6, &end); return !*end; }
    if (!strncmp(tok, "axis:", 5)) {
        out->kind = N64C_BIND_PAD_AXIS;
        out->code = (short)parse_int(tok + 5, &end);
        if (*end != '+' && *end != '-') return 0;
        out->dir = *end == '+' ? 1 : -1;
        return !end[1];
    }
    if (!strncmp(tok, "look:", 5) && (tok[5] == 'x' || tok[5] == 'y') && (tok[6] == '+' || tok[6] == '-') && !tok[7]) {
        out->kind = N64C_BIND_MOUSE_LOOK;
        out->code = tok[5] == 'y';
        out->dir = tok[6] == '+' ? 1 : -1;
        return 1;
    }
    return 0;
}
static void fail(char *err, size_t cap, int line, const char *why, const char *what)
{
    if (err && cap) snprintf(err, cap, "line %d: %s%s%s", line, why, *what ? " " : "", what);
}
int n64c_binds_parse(N64cBinds *binds, const char *text, char *err, size_t errcap)
{
    N64cBinds work = *binds;
    int player = -1, line_no = 0;
    const char *p = text;
    while (*p) {
        char line[256];
        size_t len = strcspn(p, "\n");
        ++line_no;
        if (len >= sizeof line) { fail(err, errcap, line_no, "line too long", ""); return 0; }
        memcpy(line, p, len);
        line[len] = 0;
        p += len + (p[len] == '\n');
        char *s = line;
        while (*s == ' ' || *s == '\t' || *s == '\r') ++s;
        size_t l = strlen(s);
        while (l && (s[l - 1] == ' ' || s[l - 1] == '\t' || s[l - 1] == '\r')) s[--l] = 0;
        if (!*s || *s == '#') continue;
        if (*s == '[') {
            if (strncmp(s, "[player", 7) || s[7] < '1' || s[7] > '0' + N64C_PLAYERS || strcmp(s + 8, "]")) {
                fail(err, errcap, line_no, "unknown section", s);
                return 0;
            }
            player = s[7] - '1';
            continue;
        }
        char *eq = strchr(s, '=');
        if (!eq) { fail(err, errcap, line_no, "expected name = value:", s); return 0; }
        *eq = 0;
        char *key = s, *val = eq + 1;
        size_t kl = strlen(key);
        while (kl && key[kl - 1] == ' ') key[--kl] = 0;
        while (*val == ' ') ++val;
        if (player < 0) {
            char *end;
            int n = parse_int(val, &end);
            int *dst = !strcmp(key, "deadzone") ? &work.deadzone : !strcmp(key, "button_threshold") ? &work.button_threshold :
                       !strcmp(key, "mouse_sensitivity") ? &work.mouse_sensitivity : NULL;
            if (!dst || *end || n < 0 || n > 32767) { fail(err, errcap, line_no, "bad setting", key); return 0; }
            *dst = n;
            continue;
        }
        int action = -1;
        for (int a = 0; a < N64C_ACT_COUNT; ++a)
            if (!strcmp(key, action_names[a])) action = a;
        if (action < 0) { fail(err, errcap, line_no, "unknown input", key); return 0; }
        N64cBind row[N64C_SLOT_COUNT];
        int count = 0;
        for (char *tok = strtok(val, " \t"); tok; tok = strtok(NULL, " \t")) {
            if (count == N64C_SLOT_COUNT || !parse_bind(tok, &row[count]) ||
                !n64c_bind_fits(n64c_bind_slot_device(count), &row[count])) {
                fail(err, errcap, line_no, count == N64C_SLOT_COUNT ? "too many slots:" : "bad bind", tok);
                return 0;
            }
            ++count;
        }
        if (count != N64C_SLOT_COUNT) { fail(err, errcap, line_no, "expected 6 slots for", key); return 0; }
        memcpy(work.player[player].slot[action], row, sizeof row);
    }
    *binds = work;
    return 1;
}
int n64c_binds_save(const N64cBinds *binds, const char *path)
{
    size_t need = n64c_binds_format(binds, NULL, 0);
    char *buf = malloc(need + 1), tmp[600];
    if (!buf) return 0;
    n64c_binds_format(binds, buf, need + 1);
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    FILE *f = fopen(tmp, "wb");
    int ok = f && fwrite(buf, 1, need, f) == need;
    if (f && fclose(f) != 0) ok = 0;
    free(buf);
    if (ok && rename(tmp, path) != 0) ok = 0;
    if (!ok) remove(tmp);
    return ok;
}
int n64c_binds_load(N64cBinds *binds, const char *path, char *err, size_t errcap)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        if (errno == ENOENT) return 1;
        if (err && errcap) snprintf(err, errcap, "cannot read %s: %s", path, strerror(errno));
        return 0;
    }
    char *text = NULL;
    size_t size = 0, cap = 0;
    for (;;) {
        if (size + 4096 > cap) {
            char *bigger = realloc(text, cap = cap ? cap * 2 : 8192);
            if (!bigger) { free(text); fclose(f); return 0; }
            text = bigger;
        }
        size_t got = fread(text + size, 1, cap - size - 1, f);
        if (!got) break;
        size += got;
    }
    fclose(f);
    text[size] = 0;
    int ok = n64c_binds_parse(binds, text, err, errcap);
    free(text);
    return ok;
}
