// SPDX-License-Identifier: GPL-3.0-or-later
// Draws the Controls page over the finished frame. SDL's built-in debug font renders into a small software surface,
// which is uploaded as one texture and blended over the window with the fixed pipeline (desktop compatibility
// context; GLES builds have no fixed pipeline and report that once).
#include "f3d_window.h"
#include "n64c_controls.h"
#include <SDL3/SDL_opengl.h>
#include <cstdio>

namespace N64c {
namespace {
constexpr int kWidth = 640, kHeight = 480, kScale = 2, kCell = 8 * kScale;
struct Gl {
    PFNGLUSEPROGRAMPROC useProgram;
    PFNGLACTIVETEXTUREPROC activeTexture;
    PFNGLBINDFRAMEBUFFERPROC bindFramebuffer;
    void (*genTextures)(GLsizei, GLuint *);
    void (*bindTexture)(GLenum, GLuint);
    void (*texImage2D)(GLenum, GLint, GLint, GLsizei, GLsizei, GLint, GLenum, GLenum, const void *);
    void (*texParameteri)(GLenum, GLenum, GLint);
    void (*enable)(GLenum);
    void (*disable)(GLenum);
    void (*blendFunc)(GLenum, GLenum);
    void (*begin)(GLenum);
    void (*end)();
    void (*vertex2f)(GLfloat, GLfloat);
    void (*texCoord2f)(GLfloat, GLfloat);
    void (*color4f)(GLfloat, GLfloat, GLfloat, GLfloat);
    void (*pushAttrib)(GLbitfield);
    void (*popAttrib)();
    void (*matrixMode)(GLenum);
    void (*pushMatrix)();
    void (*popMatrix)();
    void (*loadIdentity)();
    void (*viewport)(GLint, GLint, GLsizei, GLsizei);
    void (*getIntegerv)(GLenum, GLint *);
} gl;
bool glReady, glFailed;
GLuint texture;
SDL_Surface *surface;
SDL_Renderer *renderer;

template <typename T> bool load(T &fn, const char *name)
{
    fn = reinterpret_cast<T>(SDL_GL_GetProcAddress(name));
    return fn != nullptr;
}
bool initGl()
{
    if (glReady || glFailed) return glReady;
    bool ok = load(gl.useProgram, "glUseProgram") && load(gl.activeTexture, "glActiveTexture") &&
              load(gl.bindFramebuffer, "glBindFramebuffer") && load(gl.genTextures, "glGenTextures") &&
              load(gl.bindTexture, "glBindTexture") && load(gl.texImage2D, "glTexImage2D") &&
              load(gl.texParameteri, "glTexParameteri") && load(gl.enable, "glEnable") && load(gl.disable, "glDisable") &&
              load(gl.blendFunc, "glBlendFunc") && load(gl.begin, "glBegin") && load(gl.end, "glEnd") &&
              load(gl.vertex2f, "glVertex2f") && load(gl.texCoord2f, "glTexCoord2f") && load(gl.color4f, "glColor4f") &&
              load(gl.pushAttrib, "glPushAttrib") && load(gl.popAttrib, "glPopAttrib") &&
              load(gl.matrixMode, "glMatrixMode") && load(gl.pushMatrix, "glPushMatrix") &&
              load(gl.popMatrix, "glPopMatrix") && load(gl.loadIdentity, "glLoadIdentity") &&
              load(gl.viewport, "glViewport") && load(gl.getIntegerv, "glGetIntegerv");
    surface = ok ? SDL_CreateSurface(kWidth, kHeight, SDL_PIXELFORMAT_RGBA32) : nullptr;
    renderer = surface ? SDL_CreateSoftwareRenderer(surface) : nullptr;
    if (!renderer) {
        std::fprintf(stderr, "controls page: no overlay on this video backend (%s)\n", ok ? SDL_GetError() : "no fixed pipeline");
        glFailed = true;
        return false;
    }
    SDL_SetRenderScale(renderer, kScale, kScale);
    gl.genTextures(1, &texture);
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
} // namespace

void DrawControlsOverlay(SDL_Window *window)
{
    if (!n64c_controls_active() || !window || !initGl()) return;
    paint();
    int w = 0, h = 0;
    SDL_GetWindowSizeInPixels(window, &w, &h);
    if (w <= 0 || h <= 0) return;
    GLint program = 0, unit = 0;
    gl.getIntegerv(GL_CURRENT_PROGRAM, &program);
    gl.getIntegerv(GL_ACTIVE_TEXTURE, &unit);
    gl.pushAttrib(GL_ALL_ATTRIB_BITS);
    gl.bindFramebuffer(GL_FRAMEBUFFER, 0);
    gl.useProgram(0);
    gl.activeTexture(GL_TEXTURE0);
    gl.viewport(0, 0, w, h);
    gl.matrixMode(GL_PROJECTION); gl.pushMatrix(); gl.loadIdentity();
    gl.matrixMode(GL_MODELVIEW); gl.pushMatrix(); gl.loadIdentity();
    gl.disable(GL_DEPTH_TEST); gl.disable(GL_CULL_FACE); gl.disable(GL_SCISSOR_TEST); gl.disable(GL_STENCIL_TEST);
    gl.enable(GL_BLEND);
    gl.blendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    gl.disable(GL_TEXTURE_2D);
    gl.color4f(0.f, 0.f, 0.f, 0.6f); // dim the whole frame, then the 4:3 page inside it
    gl.begin(GL_QUADS); gl.vertex2f(-1, -1); gl.vertex2f(1, -1); gl.vertex2f(1, 1); gl.vertex2f(-1, 1); gl.end();
    gl.bindTexture(GL_TEXTURE_2D, texture);
    gl.texParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    gl.texParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    gl.texImage2D(GL_TEXTURE_2D, 0, GL_RGBA, kWidth, kHeight, 0, GL_RGBA, GL_UNSIGNED_BYTE, surface->pixels);
    gl.enable(GL_TEXTURE_2D);
    gl.color4f(1.f, 1.f, 1.f, 1.f);
    float sx = 1.f, sy = 1.f, aspect = (float)w / (float)h * 3.f / 4.f; // fit 4:3 inside the window
    if (aspect > 1.f) sx = 1.f / aspect; else sy = aspect;
    gl.begin(GL_QUADS);
    gl.texCoord2f(0, 1); gl.vertex2f(-sx, -sy);
    gl.texCoord2f(1, 1); gl.vertex2f(sx, -sy);
    gl.texCoord2f(1, 0); gl.vertex2f(sx, sy);
    gl.texCoord2f(0, 0); gl.vertex2f(-sx, sy);
    gl.end();
    gl.matrixMode(GL_PROJECTION); gl.popMatrix();
    gl.matrixMode(GL_MODELVIEW); gl.popMatrix();
    gl.popAttrib();
    gl.useProgram((GLuint)program);
    gl.activeTexture((GLenum)unit);
}
} // namespace N64c
