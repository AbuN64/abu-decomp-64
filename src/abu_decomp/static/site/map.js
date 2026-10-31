// Site region "map": a canvas of one box per function row of /api/map, coloured by progress (asm < exact < named; a
// judged call is green when it behaves, red when it differs) and lit by the filter chips (all unchecked = all dark).
// mount(el, ctx): ctx = {contract, api(path), go(route), on(kind, fn)}. Live changes arrive on ctx.on (the feed's
// push channel); the map refetches when one lands and redraws only when the answer's version moved.
export const CELL = 6; // box pitch in px
export const GAP = 1;
export const LIVE_KINDS = ["function.matched", "case.diffed", "feedback.filed", "run.ended"]; // events that move the rows

// The bitmask of the checked filters (`filters` is the contract's [{key, bit}]).
export const maskOf = (filters, checked) => filters.reduce((m, f) => (checked.has(f.key) ? m | f.bit : m), 0);
// A box is lit when its flags share a bit with the checked filters; a zero mask lights nothing.
export const isLit = (flags, mask) => (flags & mask) !== 0;
// The progress colour key of a box: a judged call over the decomp state (the same rule as site_map.colour_key).
export function colourKey(state, flags, bit) {
  if (flags & bit.differs) return "differs";
  if (flags & bit.behaves) return "behaves";
  return state in { asm: 1, fuzzy: 1, exact: 1, named: 1 } ? state : "asm";
}
// Grid of `n` boxes in `width` px: columns, rows and the canvas height.
export function layout(n, width) {
  const cols = Math.max(1, Math.floor(width / CELL));
  const rows = Math.ceil(n / cols);
  return { cols, rows, height: rows * CELL };
}
// The checked filters are the page's `lit` query (`#/?lit=named,exact`), in contract order, so a view is a URL.
export const litParam = (filters, checked) => filters.filter(f => checked.has(f.key)).map(f => f.key).join(",");
export function litFrom(hash, filters) {
  const wanted = new Set((new URLSearchParams(hash.split("?")[1] || "").get("lit") || "").split(","));
  return new Set(filters.filter(f => wanted.has(f.key)).map(f => f.key));
}
export const boxAt = (x, y, cols) => Math.floor(y / CELL) * cols + Math.floor(x / CELL);

const h = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) k === "class" ? (n.className = v) : n.setAttribute(k, v);
  n.append(...kids);
  return n;
};

export function mount(el, ctx) {
  const filters = ctx.contract.filters;
  const checked = litFrom(location.hash, filters);
  const state = { map: null, bit: {}, mask: maskOf(filters, checked) };
  const canvas = h("canvas", { class: "map-canvas" });
  const name = h("div", { class: "map-name mono dim" }, " ");
  const chips = filters.map(f => {
    const chip = h("button", { class: "map-chip", type: "button", "aria-pressed": String(checked.has(f.key)), "data-filter": f.key, title: f.label }, f.key);
    chip.addEventListener("click", () => {
      checked.has(f.key) ? checked.delete(f.key) : checked.add(f.key);
      chip.setAttribute("aria-pressed", String(checked.has(f.key)));
      state.mask = maskOf(filters, checked);
      const query = new URLSearchParams(location.hash.split("?")[1] || "");
      if (checked.size) query.set("lit", litParam(filters, checked)); else query.delete("lit");
      history.replaceState(null, "", `${location.pathname}${location.search}${location.hash.split("?")[0] || "#/"}${query.size ? `?${query}` : ""}`);
      draw();
    });
    return chip;
  });
  el.replaceChildren(
    h("style", {}, `
      .map-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
      .map-chip { height: 26px; padding: 0 10px; border-radius: 13px; background: var(--raised); color: var(--dim); font: 12px var(--mono); border: 1px solid var(--line); }
      .map-chip[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
      .map-canvas { display: block; width: 100%; border-radius: 6px; background: var(--raised); }
      .map-name { height: 20px; margin-top: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }`),
    h("h2", {}, "Map"), h("div", { class: "map-chips" }, ...chips), canvas, name);

  function draw() {
    const rows = state.map ? state.map.rows : [];
    const width = Math.floor(canvas.parentElement.clientWidth || canvas.clientWidth || 300);
    const { cols, height } = layout(rows.length, width);
    canvas.width = width;
    canvas.height = height;
    const style = getComputedStyle(document.documentElement);
    const paint = key => style.getPropertyValue(state.map.colours[key]).trim();
    const dark = style.getPropertyValue("--line").trim();
    const g = canvas.getContext("2d");
    g.clearRect(0, 0, width, height);
    rows.forEach(([, , st, flags], i) => {
      g.fillStyle = isLit(flags, state.mask) ? paint(colourKey(st, flags, state.bit)) : dark; // dark unless a checked filter selects it
      g.fillRect((i % cols) * CELL, Math.floor(i / cols) * CELL, CELL - GAP, CELL - GAP);
    });
    canvas.dataset.version = state.map.version; // ready marker: the canvas shows this answer under this mask
    canvas.dataset.mask = String(state.mask);
  }

  async function load() {
    const map = await ctx.api("/api/map");
    if (!map.rows) {
      el.dataset.error = String(map.error || "unavailable");
      return;
    }
    delete el.dataset.error;
    if (state.map && state.map.version === map.version) return;
    state.map = map;
    state.bit = Object.fromEntries(map.filters.map(f => [f.key, f.bit]));
    draw();
  }
  canvas.addEventListener("mousemove", e => {
    const rect = canvas.getBoundingClientRect(), { cols } = layout(state.map ? state.map.rows.length : 0, canvas.width);
    const row = state.map && state.map.rows[boxAt((e.clientX - rect.left) * (canvas.width / rect.width), (e.clientY - rect.top) * (canvas.width / rect.width), cols)];
    name.textContent = row ? `${row[0]} · ${row[2]} · ${row[1]}` : " ";
    canvas.dataset.hover = row ? row[0] : "";
  });
  canvas.addEventListener("click", e => {
    if (canvas.dataset.hover) ctx.go(`fn/${canvas.dataset.hover}`);
  });
  for (const kind of LIVE_KINDS) ctx.on(kind, load);
  load();
}
