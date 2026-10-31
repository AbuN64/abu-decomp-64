// Site region "play" (play and feedback). Stub until card RUNNER-PLAY builds it (lanes/RUNNER-REDO/SPLIT.tsv).
// mount(el, ctx): ctx = {contract, api(path), go(route), on(kind, fn), stub(what, card), route: {name, arg, query}}.
export function mount(el, ctx) {
  el.replaceChildren(ctx.stub("play and feedback", "RUNNER-PLAY"));
}
