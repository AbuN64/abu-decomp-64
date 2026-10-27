// SPDX-License-Identifier: GPL-3.0-or-later
// Draws the Controls page and the touch zones over the finished frame. SDL's built-in debug font renders into a small
// software surface, which is uploaded as one texture; both are blended over the window with one small shader program,
// so desktop GL and OpenGL ES (Android) take the same path. Every piece of GL state touched is saved and restored
// around the draw, because the Fast3D backend caches its own.
#include "f3d_controls_overlay.h"
#include "n64c_controls.h"
#include "n64c_touch.h"
#include <SDL3/SDL_opengl.h>
#include <cmath>
#include <cstdio>

namespace N64c {
namespace {
constexpr int kWidth = 640, kHeight = 480, kScale = 2, kCell = 8 * kScale;
constexpr int kFanMax = 32;
struct Gl {
    void (*genTextures)(GLsizei, GLuint *);
    void (*bindTexture)(GLenum, GLuint);
    void (*activeTexture)(GLenum);
    void (*texImage2D)(GLenum, GLint, GLint, GLsizei, GLsizei, GLint, GLenum, GLenum, const void *);
    void (*texParameteri)(GLenum, GLenum, GLint);
    void (*enable)(GLenum);
    void (*disable)(GLenum);
    GLboolean (*isEnabled)(GLenum);
    void (*blendFuncSeparate)(GLenum, GLenum, GLenum, GLenum);
    void (*colorMask)(GLboolean, GLboolean, GLboolean, GLboolean);
    void (*viewport)(GLint, GLint, GLsizei, GLsizei);
    void (*getIntegerv)(GLenum, GLint *);
    void (*getBooleanv)(GLenum, GLboolean *);
    void (*bindFramebuffer)(GLenum, GLuint);
    GLuint (*createShader)(GLenum);
    void (*shaderSource)(GLuint, GLsizei, const GLchar *const *, const GLint *);
    void (*compileShader)(GLuint);
    void (*getShaderiv)(GLuint, GLenum, GLint *);
    void (*getShaderInfoLog)(GLuint, GLsizei, GLsizei *, GLchar *);
    GLuint (*createProgram)();
    void (*attachShader)(GLuint, GLuint);
    void (*linkProgram)(GLuint);
    void (*getProgramiv)(GLuint, GLenum, GLint *);
    void (*getProgramInfoLog)(GLuint, GLsizei, GLsizei *, GLchar *);
    void (*deleteShader)(GLuint);
    void (*useProgram)(GLuint);
    GLint (*getUniformLocation)(GLuint, const GLchar *);
    void (*uniform1i)(GLint, GLint);
    void (*uniform1f)(GLint, GLfloat);
    void (*uniform4f)(GLint, GLfloat, GLfloat, GLfloat, GLfloat);
    void (*genVertexArrays)(GLsizei, GLuint *);
    void (*bindVertexArray)(GLuint);
    void (*genBuffers)(GLsizei, GLuint *);
    void (*bindBuffer)(GLenum, GLuint);
    void (*bufferData)(GLenum, GLsizeiptr, const void *, GLenum);
    void (*enableVertexAttribArray)(GLuint);
    void (*vertexAttribPointer)(GLuint, GLint, GLenum, GLboolean, GLsizei, const void *);
    void (*drawArrays)(GLenum, GLint, GLsizei);
} gl;
bool glReady, glFailed;
GLuint texture, program, vao, vbo;
GLint uColor = -1, uUseTexture = -1;
SDL_Surface *surface;
SDL_Renderer *renderer;

template <typename T> bool load(T &fn, const char *name)
{
    fn = reinterpret_cast<T>(SDL_GL_GetProcAddress(name));
    return fn != nullptr;
}
bool loadAll()
{
    return load(gl.genTextures, "glGenTextures") && load(gl.bindTexture, "glBindTexture") &&
           load(gl.activeTexture, "glActiveTexture") && load(gl.texImage2D, "glTexImage2D") &&
           load(gl.texParameteri, "glTexParameteri") && load(gl.enable, "glEnable") && load(gl.disable, "glDisable") &&
           load(gl.isEnabled, "glIsEnabled") && load(gl.blendFuncSeparate, "glBlendFuncSeparate") &&
           load(gl.colorMask, "glColorMask") && load(gl.viewport, "glViewport") && load(gl.getIntegerv, "glGetIntegerv") &&
           load(gl.getBooleanv, "glGetBooleanv") && load(gl.bindFramebuffer, "glBindFramebuffer") &&
           load(gl.createShader, "glCreateShader") && load(gl.shaderSource, "glShaderSource") &&
           load(gl.compileShader, "glCompileShader") && load(gl.getShaderiv, "glGetShaderiv") &&
           load(gl.getShaderInfoLog, "glGetShaderInfoLog") && load(gl.createProgram, "glCreateProgram") &&
           load(gl.attachShader, "glAttachShader") && load(gl.linkProgram, "glLinkProgram") &&
           load(gl.getProgramiv, "glGetProgramiv") && load(gl.getProgramInfoLog, "glGetProgramInfoLog") &&
           load(gl.deleteShader, "glDeleteShader") && load(gl.useProgram, "glUseProgram") &&
           load(gl.getUniformLocation, "glGetUniformLocation") && load(gl.uniform1i, "glUniform1i") &&
           load(gl.uniform1f, "glUniform1f") && load(gl.uniform4f, "glUniform4f") &&
           load(gl.genVertexArrays, "glGenVertexArrays") && load(gl.bindVertexArray, "glBindVertexArray") &&
           load(gl.genBuffers, "glGenBuffers") && load(gl.bindBuffer, "glBindBuffer") &&
           load(gl.bufferData, "glBufferData") && load(gl.enableVertexAttribArray, "glEnableVertexAttribArray") &&
           load(gl.vertexAttribPointer, "glVertexAttribPointer") && load(gl.drawArrays, "glDrawArrays");
}
const char *const kVertex =
    "layout(location = 0) in vec2 aPos;\n"
    "layout(location = 1) in vec2 aUv;\n"
    "out vec2 vUv;\n"
    "void main() { vUv = aUv; gl_Position = vec4(aPos, 0.0, 1.0); }\n";
const char *const kFragment =
    "in vec2 vUv;\n"
    "uniform sampler2D uTex;\n"
    "uniform vec4 uColor;\n"
    "uniform float uUseTex;\n"
    "out vec4 oColor;\n"
    "void main() { oColor = uUseTex > 0.5 ? uColor * texture(uTex, vUv) : uColor; }\n";
bool compile(GLenum kind, const char *header, const char *body, GLuint &shader)
{
    const char *parts[2] = {header, body};
    shader = gl.createShader(kind);
    gl.shaderSource(shader, 2, parts, nullptr);
    gl.compileShader(shader);
    GLint ok = 0;
    gl.getShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[512] = "";
        gl.getShaderInfoLog(shader, sizeof log, nullptr, log);
        std::fprintf(stderr, "controls page: shader: %s\n", log);
    }
    return ok != 0;
}
bool buildProgram()
{
    int profile = 0;
    SDL_GL_GetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, &profile);
    const char *header = (profile & SDL_GL_CONTEXT_PROFILE_ES) ? "#version 300 es\nprecision mediump float;\n" : "#version 330\n";
    GLuint vs = 0, fs = 0;
    if (!compile(GL_VERTEX_SHADER, header, kVertex, vs) || !compile(GL_FRAGMENT_SHADER, header, kFragment, fs)) return false;
    program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    GLint ok = 0;
    gl.getProgramiv(program, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[512] = "";
        gl.getProgramInfoLog(program, sizeof log, nullptr, log);
        std::fprintf(stderr, "controls page: link: %s\n", log);
        return false;
    }
    uColor = gl.getUniformLocation(program, "uColor");
    uUseTexture = gl.getUniformLocation(program, "uUseTex");
    return true;
}
bool initGl()
{
    if (glReady || glFailed) return glReady;
    GLint savedVao = 0, savedBuffer = 0;
    bool ok = loadAll();
    if (ok) {
        gl.getIntegerv(GL_VERTEX_ARRAY_BINDING, &savedVao);
        gl.getIntegerv(GL_ARRAY_BUFFER_BINDING, &savedBuffer);
        ok = buildProgram();
    }
    surface = ok ? SDL_CreateSurface(kWidth, kHeight, SDL_PIXELFORMAT_RGBA32) : nullptr;
    renderer = surface ? SDL_CreateSoftwareRenderer(surface) : nullptr;
    if (!renderer) {
        std::fprintf(stderr, "controls page: no overlay on this video backend (%s)\n", ok ? SDL_GetError() : "GL entry points missing");
        glFailed = true;
        return false;
    }
    SDL_SetRenderScale(renderer, kScale, kScale);
    gl.genTextures(1, &texture);
    gl.genVertexArrays(1, &vao);
    gl.genBuffers(1, &vbo);
    gl.bindVertexArray(vao);
    gl.bindBuffer(GL_ARRAY_BUFFER, vbo);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), nullptr);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), reinterpret_cast<const void *>(2 * sizeof(float)));
    gl.bindVertexArray((GLuint)savedVao);
    gl.bindBuffer(GL_ARRAY_BUFFER, (GLuint)savedBuffer);
    glReady = true;
    return true;
}
void paint()
{
    N64cControlsLine lines[N64C_CONTROLS_MAX_LINES];
    int cursor = -1;
    int count = n64c_controls_lines(lines, N64C_CONTROLS_MAX_LINES, &cursor);
    SDL_SetRenderDrawColor(renderer, 8, 10, 24, 235);
    SDL_RenderClear(renderer);
    SDL_SetRenderScale(renderer, 1, 1);
    if (cursor >= 0) {
        SDL_FRect bar = {0, (float)(cursor * kCell), (float)kWidth, (float)kCell};
        SDL_SetRenderDrawColor(renderer, 40, 70, 150, 255);
        SDL_RenderFillRect(renderer, &bar);
    }
    SDL_SetRenderScale(renderer, kScale, kScale);
    for (int i = 0; i < count; ++i) {
        SDL_SetRenderDrawColor(renderer, i == 0 ? 255 : 230, i == 0 ? 220 : 230, i == 0 ? 80 : 230, 255);
        SDL_RenderDebugText(renderer, 0, (float)(i * 8), lines[i].text);
    }
    SDL_FlushRenderer(renderer);
}
struct Saved {
    GLint program, unit, texture, vao, buffer, framebuffer, viewport[4], blend[4];
    GLboolean colorMask[4], blendOn, depth, cull, scissor, stencil;
};
Saved saved;
const GLenum kToggles[] = {GL_BLEND, GL_DEPTH_TEST, GL_CULL_FACE, GL_SCISSOR_TEST, GL_STENCIL_TEST};
void enter(int w, int h)
{
    Saved &s = saved;
    gl.getIntegerv(GL_CURRENT_PROGRAM, &s.program);
    gl.getIntegerv(GL_ACTIVE_TEXTURE, &s.unit);
    gl.getIntegerv(GL_VERTEX_ARRAY_BINDING, &s.vao);
    gl.getIntegerv(GL_ARRAY_BUFFER_BINDING, &s.buffer);
    gl.getIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &s.framebuffer);
    gl.getIntegerv(GL_VIEWPORT, s.viewport);
    gl.getIntegerv(GL_BLEND_SRC_RGB, &s.blend[0]);
    gl.getIntegerv(GL_BLEND_DST_RGB, &s.blend[1]);
    gl.getIntegerv(GL_BLEND_SRC_ALPHA, &s.blend[2]);
    gl.getIntegerv(GL_BLEND_DST_ALPHA, &s.blend[3]);
    gl.getBooleanv(GL_COLOR_WRITEMASK, s.colorMask);
    s.blendOn = gl.isEnabled(GL_BLEND); s.depth = gl.isEnabled(GL_DEPTH_TEST); s.cull = gl.isEnabled(GL_CULL_FACE);
    s.scissor = gl.isEnabled(GL_SCISSOR_TEST); s.stencil = gl.isEnabled(GL_STENCIL_TEST);
    gl.activeTexture(GL_TEXTURE0);
    gl.getIntegerv(GL_TEXTURE_BINDING_2D, &s.texture);
    gl.bindFramebuffer(GL_FRAMEBUFFER, 0);
    gl.viewport(0, 0, w, h);
    gl.colorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    for (GLenum t : kToggles) gl.disable(t);
    gl.enable(GL_BLEND);
    gl.blendFuncSeparate(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
    gl.useProgram(program);
    gl.uniform1i(gl.getUniformLocation(program, "uTex"), 0);
    gl.bindVertexArray(vao);
    gl.bindBuffer(GL_ARRAY_BUFFER, vbo);
}
void toggle(GLenum cap, GLboolean on) { on ? gl.enable(cap) : gl.disable(cap); }
void leave()
{
    const Saved &s = saved;
    gl.bindBuffer(GL_ARRAY_BUFFER, (GLuint)s.buffer);
    gl.bindVertexArray((GLuint)s.vao);
    gl.useProgram((GLuint)s.program);
    gl.bindTexture(GL_TEXTURE_2D, (GLuint)s.texture);
    gl.activeTexture((GLenum)s.unit);
    gl.bindFramebuffer(GL_FRAMEBUFFER, (GLuint)s.framebuffer);
    gl.viewport(s.viewport[0], s.viewport[1], s.viewport[2], s.viewport[3]);
    gl.colorMask(s.colorMask[0], s.colorMask[1], s.colorMask[2], s.colorMask[3]);
    gl.blendFuncSeparate((GLenum)s.blend[0], (GLenum)s.blend[1], (GLenum)s.blend[2], (GLenum)s.blend[3]);
    toggle(GL_BLEND, s.blendOn); toggle(GL_DEPTH_TEST, s.depth); toggle(GL_CULL_FACE, s.cull);
    toggle(GL_SCISSOR_TEST, s.scissor); toggle(GL_STENCIL_TEST, s.stencil);
}
// One fan of (x, y, u, v) vertices in clip space, flat colour or the page texture times the colour.
void fan(const float *verts, int count, float r, float g, float b, float a, bool textured)
{
    gl.uniform4f(uColor, r, g, b, a);
    gl.uniform1f(uUseTexture, textured ? 1.f : 0.f);
    gl.bufferData(GL_ARRAY_BUFFER, (GLsizeiptr)(count * 4 * sizeof(float)), verts, GL_STREAM_DRAW);
    gl.drawArrays(GL_TRIANGLE_FAN, 0, count);
}
// The touch zones: translucent discs at their layout positions, brighter while pressed or dragged.
void drawTouch(SDL_Window *window)
{
    int w = 0, h = 0;
    SDL_GetWindowSizeInPixels(window, &w, &h);
    if (w <= 0 || h <= 0) return;
    enter(w, h);
    const unsigned active = n64c_touch_active_zones();
    const bool editing = n64c_touch_editing() != 0;
    const N64cTouchLayout *layout = n64c_touch_layout();
    const float aspect = (float)w / (float)h;
    for (int i = 0; i < N64C_TOUCH_ZONES; ++i) {
        const N64cTouchZone &z = layout->zone[i];
        const float cx = z.x * 2.f - 1.f, cy = 1.f - z.y * 2.f, rx = z.r * 2.f, ry = rx * aspect;
        float v[(kFanMax + 2) * 4] = {cx, cy, 0, 0};
        for (int k = 0; k <= 24; ++k) {
            v[(k + 1) * 4] = cx + rx * std::cos(k * 6.2831853f / 24.f);
            v[(k + 1) * 4 + 1] = cy + ry * std::sin(k * 6.2831853f / 24.f);
            v[(k + 1) * 4 + 2] = v[(k + 1) * 4 + 3] = 0;
        }
        fan(v, 26, 1.f, editing ? 0.8f : 1.f, editing ? 0.2f : 1.f, (active >> i) & 1 ? 0.55f : 0.22f, false);
    }
    leave();
}
} // namespace

void DrawControlsOverlay(SDL_Window *window)
{
    if (!window || !(n64c_controls_active() || n64c_touch_seen()) || !initGl()) return;
    if (!n64c_controls_active()) {
        drawTouch(window);
        return;
    }
    paint();
    int w = 0, h = 0;
    SDL_GetWindowSizeInPixels(window, &w, &h);
    if (w <= 0 || h <= 0) return;
    enter(w, h);
    const float whole[16] = {-1, -1, 0, 0, 1, -1, 0, 0, 1, 1, 0, 0, -1, 1, 0, 0};
    fan(whole, 4, 0.f, 0.f, 0.f, 0.6f, false); // dim the whole frame, then the 4:3 page inside it
    gl.bindTexture(GL_TEXTURE_2D, texture);
    gl.texParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    gl.texParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    gl.texParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    gl.texParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    gl.texImage2D(GL_TEXTURE_2D, 0, GL_RGBA, kWidth, kHeight, 0, GL_RGBA, GL_UNSIGNED_BYTE, surface->pixels);
    float sx = 1.f, sy = 1.f, aspect = (float)w / (float)h * 3.f / 4.f; // fit 4:3 inside the window
    if (aspect > 1.f) sx = 1.f / aspect; else sy = aspect;
    const float page[16] = {-sx, -sy, 0, 1, sx, -sy, 1, 1, sx, sy, 1, 0, -sx, sy, 0, 0};
    fan(page, 4, 1.f, 1.f, 1.f, 1.f, true);
    leave();
}
} // namespace N64c
