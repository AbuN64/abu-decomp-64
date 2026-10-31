// Site region "fn" (function screen): the function's code with line counts, its runs grouped by kind, and the cases
// table (inputs, port output, original output, first divergence), all typed records from /api/fn/NAME.
// mount(el, ctx): ctx = {contract, api(path), go(route), on(kind, fn), route: {name, arg, query}}.
// Nothing here writes prose: every cell is a field of a record; updates arrive through ctx.on (the shell's SSE push).

const STYLE = `
.fn { display: grid; gap: 16px; }
.fn h2, .fn h3 { margin: 0; font-size: 15px; font-weight: 600; }
.fn .head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px; }
.fn .head h2 { font: 600 18px var(--mono); text-transform: none; letter-spacing: 0; }
.fn .tag { font: 12px var(--mono); padding: 2px 8px; border-radius: 999px; background: var(--raised); color: var(--dim); }
.fn .tag.same, .fn .tag.good { color: var(--b-ok); }
.fn .tag.bad, .fn .tag.diverged, .fn .tag.differs { color: var(--b-diff); }
.fn .panel { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px; overflow: auto; }
.fn .code { font: 12.5px/1.5 var(--mono); white-space: pre; max-height: 320px; }
.fn .code .row { display: flex; gap: 12px; }
.fn .code .count { flex: none; width: 5em; text-align: right; color: var(--faint); }
.fn .code .row.hit .count { color: var(--b-ok); }
.fn .code .row.miss .count { color: var(--b-diff); }
.fn .tok-k, .fn .tok-kt, .fn .tok-kd, .fn .tok-kr { color: var(--accent); }
.fn .tok-c, .fn .tok-c1, .fn .tok-cm, .fn .tok-cp, .fn .tok-cpf { color: var(--faint); }
.fn .tok-s, .fn .tok-s1, .fn .tok-s2, .fn .tok-sc { color: var(--warn); }
.fn .tok-m, .fn .tok-mi, .fn .tok-mf, .fn .tok-mh { color: var(--s-named); }
.fn .group { display: grid; gap: 8px; }
.fn .runs { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
.fn .run { display: grid; gap: 4px; padding: 10px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  text-decoration: none; }
.fn a.run:hover { border-color: var(--accent); }
.fn .run .meta { display: flex; justify-content: space-between; font: 12px var(--mono); color: var(--dim); }
.fn table { width: 100%; border-collapse: collapse; font: 12.5px var(--mono); }
.fn th { text-align: left; font: 600 12px var(--sans); color: var(--dim); }
.fn th, .fn td { padding: 6px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
.fn td ul { margin: 0; padding: 0; list-style: none; }
.fn tr.diverged td:last-child { color: var(--b-diff); }
.fn .shot { width: 100%; border-radius: 6px; background: var(--raised); }
.fn .pair { color: var(--dim); }
`;

const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  e.append(...kids.filter(k => k !== null && k !== undefined));
  return e;
};
const text = v => v === null || v === undefined ? "–" : typeof v === "string" ? v : typeof v === "number" ? v.toLocaleString("en-US") : JSON.stringify(v);

// Pygments' token spans come from the bake; their short class names collide with the shell's, so they are prefixed.
export function codeLines(html) {
  return html.map(markup => {
    const holder = document.createElement("template");
    holder.innerHTML = markup;
    for (const span of holder.content.querySelectorAll("span")) span.className = `tok-${span.className}`;
    return holder.content;
  });
}

// One gutter entry: null (no code), [count] or [count, branches taken, branches total].
export function countCell(entry) {
  if (!entry) return { text: "", state: "" };
  const [count, taken, total] = entry;
  return { text: total ? `${text(count)} ${taken}/${total}` : text(count), state: count ? "hit" : "miss" };
}

// Runs grouped by kind in the contract's order; a kind with no run is left out.
export function groupRuns(runs, kinds) {
  return kinds.map(kind => ({ kind, runs: runs.filter(r => r.kind === kind) })).filter(g => g.runs.length);
}

// A TAS run's link: its stream at its first poll of the function, else the stream's start.
export function runHref(run) {
  const base = `#/stream/${encodeURIComponent(run.id)}`;
  return run.first === null || run.first === undefined ? base : `${base}?poll=${run.first}`;
}

export function firstDivergence(c) {
  return c.divergence.length ? c.divergence[0] : null;
}

function codePanel(rec) {
  const lines = codeLines(rec.html);
  const rows = lines.map((content, i) => {
    const cell = countCell(rec.lines[i]);
    return h("div", { class: `row ${cell.state}` }, h("span", { class: "count" }, cell.text), h("span", {}, content));
  });
  return h("div", { class: "panel code" }, ...(rows.length ? rows : [h("span", { class: "dim" }, "–")]));
}

// The recorded frames of both sides at the run's first poll of the function (the stream's files, contract).
export function previewSrc(run, side) {
  return `/stream/${encodeURIComponent(run.id)}/${side}/${run.first}.jpg`;
}

function previews(run) {
  if (run.first === null || run.first === undefined) return [];
  return run.sides.map(side => h("img", { class: "shot", loading: "lazy", alt: side, src: previewSrc(run, side) }));
}

function runCard(run) {
  const body = [
    h("div", { class: "meta" }, h("span", {}, run.lane), h("span", {}, run.status)),
    h("b", { class: "mono" }, run.movie),
    h("div", { class: "meta" }, h("span", {}, `${text(run.poll)} / ${text(run.polls)}`), h("span", {}, text(run.outcome))),
  ];
  if (run.kind === "tas") return h("a", { class: "run", href: runHref(run) }, ...(run.stream ? previews(run) : []), ...body);
  return h("div", { class: "run" }, ...body);
}

function runsPanel(rec, kinds) {
  return h("div", { class: "group" }, h("h3", {}, "Runs"),
    ...groupRuns(rec.runs, kinds).map(g => h("div", { class: "group", "data-kind": g.kind },
      h("span", { class: "tag" }, g.kind), h("div", { class: "runs" }, ...g.runs.map(runCard)))));
}

function side(output) {
  if (!output) return h("span", { class: "dim" }, "–");
  return h("ul", {}, ...["status", "return", "writes"].filter(k => output[k] !== null && output[k] !== undefined)
    .map(k => h("li", {}, h("span", { class: "pair" }, `${k} `), text(output[k]))));
}

function listing(values) {
  return h("ul", {}, ...(values.length ? values.map(v => h("li", {}, text(v))) : [h("li", { class: "dim" }, "–")]));
}

function caseRow(c) {
  const d = firstDivergence(c);
  return h("tr", { class: c.divergence.length ? "diverged" : "same", "data-ordinal": String(c.ordinal) },
    h("td", {}, `${text(c.caller)} #${text(c.ordinal)}`),
    h("td", {}, listing(c.inputs.port)),
    h("td", {}, side(c.outputs.port)),
    h("td", {}, side(c.outputs.original)),
    h("td", {}, d ? `${d.field} ${d.kind}: ${text(d.port)} ≠ ${text(d.original)}` : "–"),
    h("td", {}, h("span", { class: `tag ${c.verdict}` }, text(c.verdict)), ` ${text(c.compared)}`));
}

function casesPanel(rec) {
  const head = ["call", "inputs", "port out", "original out", "first divergence", "verdict"];
  return h("div", { class: "group" }, h("h3", {}, "Cases"),
    h("div", { class: "panel" }, h("table", {},
      h("thead", {}, h("tr", {}, ...head.map(title => h("th", {}, title)))),
      h("tbody", {}, ...(rec.cases.length ? rec.cases.map(caseRow) : [h("tr", {}, h("td", { colspan: String(head.length), class: "dim" }, "–"))])))));
}

function render(rec, kinds) {
  return h("div", { class: "fn" },
    h("div", { class: "head" }, h("h2", {}, rec.name),
      ...[rec.state, rec.behaviour, rec.file && `${rec.file}:${text(rec.first)}`, `${text(rec.size)} B`].filter(Boolean).map(t => h("span", { class: `tag ${t}` }, t))),
    codePanel(rec), runsPanel(rec, kinds), casesPanel(rec));
}

// Events that can change this screen: the function is a run's or case's target, or the event's run is one we show.
export function concerns(ev, rec) {
  const f = ev.fields;
  return f.target === rec.name || (f.run && rec.runs.some(r => r.id === f.run)) || rec.movies.includes(f.movie);
}

export function mount(el, ctx) {
  if (!document.getElementById("fn-style")) document.head.append(h("style", { id: "fn-style" }, STYLE));
  const name = ctx.route.arg;
  const kinds = ctx.contract.run_kinds;
  let rec = null, loading = false, again = false;

  async function load() {
    if (loading) { again = true; return; }
    loading = true;
    try {
      do {
        again = false;
        rec = await ctx.api(`/api/fn/${encodeURIComponent(name)}`);
        el.replaceChildren(rec.error ? h("div", { class: "dim" }, rec.error) : render(rec, kinds));
      } while (again);
    } finally { loading = false; }
  }

  // The shell keeps listeners for the page's life: this one acts only while this screen is the one shown.
  for (const kind of Object.keys(ctx.contract.templates)) {
    ctx.on(kind, ev => { if (el.isConnected && ctx.route.arg === name && rec && !rec.error && concerns(ev, rec)) load(); });
  }
  return load();
}
