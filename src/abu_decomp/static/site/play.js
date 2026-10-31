// Site region "play": start a human run, drive it with keys or a gamepad, and flag it at a poll (card RUNNER-PLAY).
// mount(el, ctx): ctx = {contract, api(path, init), go(route), on(kind, fn), route: {name, arg, query}}.
// Routes: #/play starts nothing by itself; #/play/RUN shows that run. Pictures come from /video/RUN (frames pushed by the
// server), poll counts from the feed's run events, flags from /api/feedback and its feedback.filed events: no polling.
const BT = { A: 0x8000, B: 0x4000, Z: 0x2000, START: 0x1000, UP: 0x0800, DOWN: 0x0400, LEFT: 0x0200, RIGHT: 0x0100,
  L: 0x0020, R: 0x0010, CUP: 0x0008, CDOWN: 0x0004, CLEFT: 0x0002, CRIGHT: 0x0001 };
const KEYS = [["ArrowUp", "stick up"], ["ArrowDown", "stick down"], ["ArrowLeft", "stick left"], ["ArrowRight", "stick right"],
  ["KeyX", "A"], ["KeyC", "B"], ["KeyZ", "Z"], ["Enter", "START"], ["KeyA", "L"], ["KeyS", "R"],
  ["KeyI", "CUP"], ["KeyK", "CDOWN"], ["KeyJ", "CLEFT"], ["KeyL", "CRIGHT"], ["KeyT", "UP"], ["KeyG", "DOWN"], ["KeyF", "LEFT"], ["KeyH", "RIGHT"]];
const PAD = [[0, "A"], [1, "B"], [2, "B"], [3, "CUP"], [4, "L"], [5, "R"], [6, "Z"], [7, "Z"], [9, "START"], [12, "UP"], [13, "DOWN"], [14, "LEFT"], [15, "RIGHT"]];
const STICK = 80, DEADZONE = 0.15, HEARTBEAT_MS = 500;
const RUN_KINDS = ["run.progress:tas", "run.progress:tas-target", "run.queued", "run.ended"];

const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) k === "class" ? (n.className = v) : n.setAttribute(k, v);
  n.append(...kids.filter(k => k != null));
  return n;
};
const post = (ctx, path, body) => ctx.api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// Pad state (button bits, stick) from held keys and the Gamepad API.
export function padState(keys, gamepads) {
  let buttons = 0, x = 0, y = 0;
  for (const [code, what] of KEYS) {
    if (!keys.has(code)) continue;
    if (what === "stick up") y += STICK; else if (what === "stick down") y -= STICK;
    else if (what === "stick left") x -= STICK; else if (what === "stick right") x += STICK;
    else buttons |= BT[what];
  }
  for (const g of gamepads) {
    if (!g) continue;
    for (const [i, what] of PAD) if (g.buttons[i] && g.buttons[i].pressed) buttons |= BT[what];
    const axis = v => Math.abs(v) < DEADZONE ? 0 : Math.round((v - Math.sign(v) * DEADZONE) / (1 - DEADZONE) * STICK);
    if (g.axes.length >= 2) { x += axis(g.axes[0]); y -= axis(g.axes[1]); }
  }
  const clamp = v => Math.max(-STICK, Math.min(STICK, Math.round(v)));
  return { buttons, x: clamp(x), y: clamp(y) };
}

// Video framing: u32 BE length (top bit = reach record), u64 BE stamp, u32 BE seq, then the JPEG.
export function framer(onFrame) {
  let buf = new Uint8Array(0);
  return chunk => {
    const joined = new Uint8Array(buf.length + chunk.length); joined.set(buf); joined.set(chunk, buf.length); buf = joined;
    let o = 0;
    while (buf.length - o >= 16) {
      const raw = new DataView(buf.buffer, buf.byteOffset + o, 4).getUint32(0), n = raw & 0x7fffffff;
      if (buf.length - o < 16 + n) break;
      if (!(raw & 0x80000000)) onFrame(buf.slice(o + 16, o + 16 + n));
      o += 16 + n;
    }
    buf = buf.slice(o);
  };
}

let current = null;  // the mounted view; a handler of an older mount sees it is no longer current and does nothing

export function mount(root, ctx) {
  current?.stop();
  const S = { run: ctx.route.arg || null, status: null, poll: 0, keys: new Set(), last: "", lastAt: 0, seq: 0, abort: null, frame: 0, flags: new Map() };
  const view = { stop: () => { S.abort?.abort(); cancelAnimationFrame(S.frame); removeEventListener("keydown", down); removeEventListener("keyup", up); } };
  current = view;
  const live = () => current === view;

  const start = el("button", { class: "btn", type: "button", id: "play-start" }, "New run");
  const flag = el("button", { class: "btn", type: "button", id: "play-flag", disabled: "" }, "Flag");
  const stop = el("button", { class: "btn alt", type: "button", id: "play-stop", disabled: "" }, "Stop");
  const tag = el("span", { class: "mono", id: "play-run" });
  const canvas = el("canvas", { id: "play-stage", width: "640", height: "480" });
  const list = el("ul", { class: "feed", id: "play-flags" });
  const dialog = flagDialog(ctx, S, () => live());
  root.replaceChildren(el("h2", {}, "Play"), el("div", { class: "playbar" }, start, flag, stop, tag), canvas, el("h2", {}, "Flags"), list, dialog);
  addStyle();

  const refresh = () => {
    const running = S.status === "running" || S.status === "queued";
    tag.textContent = S.run ? `${S.run} · ${S.status || "?"} · poll ${S.poll.toLocaleString("en-US")}` : "";
    flag.disabled = !S.run; stop.disabled = !(S.run && running);
  };
  const renderFlags = () => list.replaceChildren(...[...S.flags.values()].sort((a, b) => b.at - a.at).map(flagRow));

  function watch(id) {
    S.run = id; S.status = "running"; S.poll = 0; refresh();
    if (location.hash !== `#/play/${encodeURIComponent(id)}`) history.replaceState(null, "", `#/play/${encodeURIComponent(id)}`);
    S.abort?.abort(); S.abort = new AbortController();
    video(id, S.abort.signal, canvas);
  }
  start.addEventListener("click", async () => {
    const got = await post(ctx, "/api/play", { label: "owner", fps_unlock: true });
    if (!got.id) return void (tag.textContent = got.error || "refused");
    watch(got.id);
  });
  stop.addEventListener("click", () => S.run && post(ctx, "/api/stop", { id: S.run }));
  flag.addEventListener("click", () => dialog.open(S.run, S.poll));

  // pad input: keys and gamepad, sent on change and as a heartbeat, only while this run is the one shown
  const driving = () => live() && S.run && S.status === "running" && !dialog.el.open;
  const send = force => {
    if (!driving()) return;
    const pad = padState(S.keys, navigator.getGamepads ? navigator.getGamepads() : []), key = `${pad.buttons} ${pad.x} ${pad.y}`, now = performance.now();
    if (key === S.last && !force && now - S.lastAt < HEARTBEAT_MS) return;
    S.last = key; S.lastAt = now; S.seq++;
    post(ctx, "/api/input", { id: S.run, seq: S.seq, buttons: pad.buttons, x: pad.x, y: pad.y, pad: 0 });
  };
  function down(e) { if (!driving() || e.ctrlKey || e.metaKey || e.altKey || !KEYS.some(k => k[0] === e.code)) return; e.preventDefault(); S.keys.add(e.code); send(true); }
  function up(e) { if (S.keys.delete(e.code)) { e.preventDefault(); send(true); } }
  const loop = () => { send(false); S.frame = requestAnimationFrame(loop); };
  addEventListener("keydown", down); addEventListener("keyup", up); loop();

  // live state: run events move the poll and status, feedback.filed adds the row
  const onRun = ev => { if (live() && ev.fields.run === S.run) { S.status = ev.kind === "run.ended" ? "done" : ev.kind === "run.queued" ? "queued" : "running"; S.poll = ev.fields.poll; refresh(); } };
  for (const kind of RUN_KINDS) ctx.on(kind, onRun);
  ctx.on("feedback.filed", async ev => { if (!live()) return; const rec = await ctx.api(`/api/feedback/${ev.fields.feedback}`); if (rec.id) { S.flags.set(rec.id, rec); renderFlags(); } });
  (async () => {
    for (const rec of (await ctx.api("/api/feedback")).feedback || []) S.flags.set(rec.id, rec);
    renderFlags();
    if (S.run) { const feed = await ctx.api("/api/feed"); for (const ev of feed.events) if (ev.fields.run === S.run && ev.kind.startsWith("run.")) onRun(ev); watch(S.run); }
  })();
  refresh();
}

// One typed row per flag: category, run, poll, input count, frames present.
function flagRow(rec) {
  return el("li", { class: "ev", "data-id": rec.id },
    el("time", {}, new Date(rec.at * 1000).toLocaleTimeString("en-GB", { hour12: false })),
    el("b", { class: "cat", "data-category": rec.category }, rec.category),
    el("span", { class: "mono" }, `${rec.run} · poll ${rec.poll.toLocaleString("en-US")} · ${rec.inputs.length} inputs · ${Object.values(rec.frames).filter(Boolean).length} frames`),
    el("span", { class: "k" }, rec.text || ""));
}

// The flag dialog: category radios from the contract, a text field, the poll it is filed at.
function flagDialog(ctx, S, live) {
  const dialog = el("dialog", { id: "play-dialog" });
  const poll = el("input", { type: "number", min: "0", id: "flag-poll", name: "poll" });
  const text = el("textarea", { id: "flag-text", name: "text", rows: "3", maxlength: "2000" });
  const status = el("span", { class: "dim", id: "flag-status" });
  const form = el("form", { method: "dialog" },
    el("h2", {}, "Flag"),
    el("label", {}, "poll ", poll),
    el("div", { class: "cats" }, ...ctx.contract.feedback_categories.map((c, i) =>
      el("label", { class: "cat" }, el("input", { type: "radio", name: "category", value: c, ...(i === 0 ? { checked: "" } : {}) }), c))),
    text,
    el("div", { class: "row" }, el("button", { class: "btn", type: "submit", id: "flag-file" }, "File"),
      el("button", { class: "btn alt", type: "button", id: "flag-cancel" }, "Cancel"), status));
  dialog.append(form);
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const category = form.elements.category.value;
    const got = await post(ctx, "/api/feedback", { run: S.run, poll: Number(poll.value), category, text: text.value });
    if (!got.id) { status.textContent = got.error || "refused"; return; }
    dialog.close();
  });
  form.querySelector("#flag-cancel").addEventListener("click", () => dialog.close());
  return { el: dialog, open: (run, at) => { if (!live() || !run) return; poll.value = String(at); text.value = ""; status.textContent = ""; dialog.showModal(); } };
}

// Stream the run's pictures: the server pushes JPEG frames; the newest one is painted, none replayed as a burst.
async function video(id, signal, canvas) {
  const paint = bytes => createImageBitmap(new Blob([bytes], { type: "image/jpeg" })).then(bmp => {
    canvas.width = bmp.width; canvas.height = bmp.height; canvas.getContext("2d").drawImage(bmp, 0, 0); bmp.close();
  });
  try {
    const res = await fetch(`/video/${encodeURIComponent(id)}`, { cache: "no-store", signal });
    if (!res.ok || !res.body) return;
    const reader = res.body.getReader();
    let newest = null;
    const feed = framer(frame => { newest = frame; });
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      feed(value);
      if (newest) { const n = newest; newest = null; await paint(n); }
    }
  } catch (error) { if (!signal.aborted) throw error; }
}

function addStyle() {
  if (document.getElementById("play-style")) return;
  document.head.append(el("style", { id: "play-style" }, `
.playbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.btn.alt { background: var(--raised); color: var(--text); }
.btn[disabled] { opacity: .4; cursor: default; }
#play-stage { width: 100%; max-width: 640px; aspect-ratio: 4 / 3; background: #000; border-radius: var(--radius); image-rendering: pixelated; }
#play-dialog { background: var(--surface); color: var(--text); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px; min-width: 360px; }
#play-dialog::backdrop { background: rgba(0, 0, 0, .55); }
#play-dialog .cats { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin: 12px 0; }
#play-dialog .cat { display: flex; gap: 6px; align-items: center; padding: 6px 10px; border-radius: 8px; background: var(--raised); font: 13px var(--mono); }
#play-dialog textarea, #play-dialog input[type=number] { width: 100%; background: var(--raised); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 8px; font: 14px var(--sans); }
#play-dialog .row { display: flex; gap: 10px; align-items: center; margin-top: 12px; }
#play-flags .cat { font: 12px var(--mono); color: var(--bad); }
`));
}
