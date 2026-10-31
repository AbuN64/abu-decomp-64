// Site region "map" (function map). Stub until card RUNNER-MAP builds it (lanes/RUNNER-REDO/SPLIT.tsv).
// mount(el, ctx): ctx = {contract, api(path), go(route), on(kind, fn), stub(what, card), route: {name, arg, query}}.
export function mount(el, ctx) {
  el.replaceChildren(ctx.stub("function map", "RUNNER-MAP"));
}
