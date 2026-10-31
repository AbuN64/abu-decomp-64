// Site region "stream": a recorded run replayed in lockstep, port beside original, keyed to the poll index.
// Route #/stream/RUN?poll=P. The clock is the poll index (60 polls per second at 1x); the page shows, for the poll
// the clock stands on, the latest recorded poll at or before it on both sides, so the two pictures always match.
// mount(el, ctx): ctx = {contract, api(path), go(route), on(kind, fn), route: {name, arg, query}}.

export const RATE = 60; // polls per second at 1x
export const SPEEDS = [1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16];
export const SIDES = ["port", "original"];

// ---------- the poll clock (pure) ----------
// state = {pos: poll position, dir: -1 rewind | 0 paused | 1 play, speed}; polls = recorded polls, ascending.

/** Index of the latest recorded poll at or before `pos` (the first one when `pos` is before them all). */
export function indexAt(polls, pos) {
  let lo = 0, hi = polls.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (polls[mid] <= pos) lo = mid; else hi = mid - 1;
  }
  return lo;
}

/** The clock after `ms` of wall time: moves `dir * RATE * speed` polls per second, pauses at either end. */
export function advance(polls, state, ms) {
  if (!state.dir || !polls.length) return state;
  const first = polls[0], last = polls[polls.length - 1];
  const pos = Math.min(last, Math.max(first, state.pos + state.dir * RATE * state.speed * ms / 1000));
  return { ...state, pos, dir: pos === first || pos === last ? 0 : state.dir };
}

/** One recorded poll forward (`by` = 1) or back (-1) from the shown one; paused. */
export function step(polls, state, by) {
  const at = Math.min(polls.length - 1, Math.max(0, indexAt(polls, state.pos) + by));
  return { ...state, pos: polls[at], dir: 0 };
}

/** The scrub bar's poll, snapped onto the recorded polls; paused. */
export function seek(polls, state, poll) {
  return { ...state, pos: polls[indexAt(polls, poll)], dir: 0 };
}

export const withSpeed = (state, speed) => ({ ...state, speed });
export const play = (state, dir) => ({ ...state, dir });
export const speedLabel = speed => speed < 1 ? `1/${1 / speed}x` : `${speed}x`;

/** The poll to start on: the `?poll=` one snapped onto the recorded polls, else the first. */
export function startPoll(polls, wanted) {
  const n = Number(wanted);
  return wanted !== undefined && wanted !== "" && Number.isFinite(n) ? polls[indexAt(polls, n)] : polls[0];
}

/** Buttons held at a poll as the names the contract lists, else the hex mask: [buttons, x, y] -> record fields. */
export const inputFields = input => input ? { buttons: input[0].toString(16).padStart(4, "0"), x: input[1], y: input[2] } : null;

// ---------- the view ----------
const h = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) v === false || v == null ? 0 : node.setAttribute(k, v === true ? "" : v);
  node.append(...kids);
  return node;
};
const icon = name => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"), use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  svg.setAttribute("class", "i"); use.setAttribute("href", `/static/icons.svg#i-${name}`); svg.append(use); return svg;
};
const CSS = `
.stream .frames { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; max-width: 980px; }
.stream figure { margin: 0; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }
.stream figcaption { display: flex; justify-content: space-between; padding: 8px 12px; font: 12px var(--mono); color: var(--dim); }
.stream img { display: block; width: 100%; aspect-ratio: 4 / 3; image-rendering: pixelated; background: #000; }
.stream .bar { display: flex; align-items: center; gap: 6px; max-width: 980px; margin-top: 12px; padding: 8px 12px; background: var(--surface);
  border: 1px solid var(--line); border-radius: var(--radius); flex-wrap: wrap; }
.stream button, .stream select { height: 32px; min-width: 32px; padding: 0 8px; border-radius: 8px; border: 1px solid var(--line); background: var(--raised);
  color: var(--text); display: inline-flex; align-items: center; justify-content: center; gap: 6px; font: 12px var(--mono); }
.stream button[aria-pressed="true"] { border-color: var(--accent); color: var(--accent); }
.stream button:hover { border-color: var(--accent); }
.stream input[type=range] { flex: 1 1 240px; accent-color: var(--accent); }
.stream .readout { font: 12px var(--mono); color: var(--dim); }
.stream .take { margin-left: auto; background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
.stream .refusal { padding: 14px 18px; border: 1px solid var(--bad); border-radius: var(--radius); font: 13px var(--mono); }
.stream .refusal b { color: var(--bad); }
`;

const refusal = rec => h("div", { class: "refusal" }, h("b", {}, rec.code), ...Object.entries(rec).filter(([k, v]) => k !== "kind" && k !== "code" && v != null).map(([k, v]) => ` ${k}=${v}`));

export function mount(el, ctx) {
  return open(el, ctx);
}

async function open(root, ctx) {
  const run = ctx.route.arg;
  root.replaceChildren(h("style", {}, CSS));
  const view = h("div", { class: "stream", "data-run": run });
  root.append(view);
  const data = await ctx.api(`/api/stream/${encodeURIComponent(run)}`);
  if (data.kind === "error") return view.append(refusal(data));
  let polls = data.polls, inputs = data.inputs;
  let state = { pos: startPoll(polls, ctx.route.query.poll), dir: 0, speed: 1 };
  let shown = -1, token = 0, last = 0, frame = 0;

  const imgs = Object.fromEntries(SIDES.map(side => [side, h("img", { alt: side })]));
  const poll = h("span", {}), count = h("span", {}), pad = h("span", {});
  const caption = side => h("figcaption", {}, h("span", {}, side), side === SIDES[0] ? poll : pad);
  const frames = h("div", { class: "frames" }, ...SIDES.map(side => h("figure", { "data-side": side }, imgs[side], caption(side))));
  const scrub = h("input", { type: "range", step: 1, "aria-label": "scrub" });
  const button = (name, label, on) => { const b = h("button", { title: label, "aria-label": label }, icon(name)); b.onclick = on; return b; };
  const speeds = h("select", { "aria-label": "speed" }, ...SPEEDS.map(s => h("option", { value: s, selected: s === 1 }, speedLabel(s))));
  const btn = {
    rewind: button("rotate-ccw", "rewind", () => set(play(state, -1))),
    back: button("chevron-left", "step back", () => set(step(polls, state, -1))),
    pause: button("square", "pause", () => set(play(state, 0))),
    play: button("play", "play", () => set(play(state, 1))),
    forward: button("chevron-right", "step", () => set(step(polls, state, 1))),
  };
  const takeover = h("button", { class: "take", title: "take over at the shown poll" }, icon("gamepad-2"), "take over");
  const result = h("span", {});
  view.append(frames, h("div", { class: "bar" }, btn.rewind, btn.back, btn.pause, btn.play, btn.forward, speeds, scrub, count, takeover, result));

  function paint() {
    const index = indexAt(polls, state.pos), at = polls[index];
    scrub.min = polls[0]; scrub.max = polls[polls.length - 1]; scrub.value = state.pos;
    count.textContent = `${index + 1}/${polls.length}`;
    for (const key of ["rewind", "pause", "play"]) btn[key].setAttribute("aria-pressed", String(state.dir === { rewind: -1, pause: 0, play: 1 }[key]));
    if (index === shown) return;
    shown = index;
    view.dataset.poll = at;
    const mine = ++token;
    const input = inputFields(inputs[index]);
    poll.textContent = `poll ${at}`;
    pad.textContent = input ? `${input.buttons} ${input.x} ${input.y}` : "";
    // Both sides swap together once both decoded, so a viewer never sees two different polls.
    Promise.all(SIDES.map(side => { const img = new Image(); img.src = `/stream/${encodeURIComponent(run)}/${side}/${at}.jpg`; return img.decode().then(() => img.src); }))
      .then(urls => { if (mine === token) SIDES.forEach((side, i) => { imgs[side].src = urls[i]; }); }, () => {});
    history.replaceState(null, "", `#/stream/${encodeURIComponent(run)}?poll=${at}`);
  }
  function tick(now) {
    frame = 0;
    state = advance(polls, state, last ? now - last : 0);
    last = now; paint();
    if (state.dir && view.isConnected) frame = requestAnimationFrame(tick);
  }
  function set(next) {
    state = next; paint(); last = 0;
    if (state.dir && !frame) frame = requestAnimationFrame(tick);
  }
  scrub.oninput = () => set(seek(polls, state, Number(scrub.value)));
  speeds.onchange = () => set(withSpeed(state, Number(speeds.value)));
  takeover.onclick = async () => {
    const out = await ctx.api("/api/play", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ from: run, at: polls[indexAt(polls, state.pos)] }) });
    if (out.id) return ctx.go(`play/${encodeURIComponent(out.id)}`);
    result.replaceChildren(refusal({ code: "play.refused", run, poll: state.pos, detail: out.error }));
  };
  // A run still going pushes its progress on the feed; the index is read again then, never on a timer.
  for (const kind of Object.keys(ctx.contract.templates).filter(k => k.startsWith("run."))) {
    ctx.on(kind, async ev => {
      if (!view.isConnected || ev.fields.run !== run) return;
      const fresh = await ctx.api(`/api/stream/${encodeURIComponent(run)}`);
      if (fresh.kind === "error") return;
      polls = fresh.polls; inputs = fresh.inputs; shown = -1; paint();
    });
  }
  paint();
}
