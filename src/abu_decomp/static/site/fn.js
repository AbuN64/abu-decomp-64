// Site region "fn" (function screen). Stub until card RUNNER-FN builds it (lanes/RUNNER-REDO/SPLIT.tsv).
// mount(el, ctx): ctx = {contract, api(path), go(route), on(kind, fn), stub(what, card), route: {name, arg, query}}.
export function mount(el, ctx) {
  el.replaceChildren(ctx.stub("function screen", "RUNNER-FN"));
}
