// Site region "stream" (stream view). Stub until card RUNNER-STREAM builds it (lanes/RUNNER-REDO/SPLIT.tsv).
// mount(el, ctx): ctx = {contract, api(path), go(route), on(kind, fn), stub(what, card), route: {name, arg, query}}.
export function mount(el, ctx) {
  el.replaceChildren(ctx.stub("stream view", "RUNNER-STREAM"));
}
