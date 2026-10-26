// Drives headless Chromium over CDP for tests/test_tas_page.py.
// usage: node drive.mjs PLAN.json   plan = {url, width, height, profile, chrome, steps: [{wait: ms} | {eval: "js"} | {shot: "path.png"} | {key: "KeyX", type: "down|up"}]}
// prints one JSON object: {results: [...], errors: [...], requests: [{url, bytes}]}
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";

const plan = JSON.parse(readFileSync(process.argv[2], "utf8"));
const port = 9333 + Math.floor(Math.random() * 500);
const ch = spawn(plan.chrome, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${plan.profile}`, "--no-first-run", "--no-sandbox",
  "--disable-gpu", `--window-size=${plan.width || 1440},${plan.height || 900}`, "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ws;
for (let i = 0; i < 150 && !ws; i++) {
  try { const l = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); const p = l.find(t => t.type === "page"); if (p) ws = new WebSocket(p.webSocketDebuggerUrl); } catch {}
  if (!ws) await sleep(200);
}
await new Promise(r => { ws.onopen = r; });
let id = 0;
const pend = new Map(), errors = [], reqs = new Map();
ws.onmessage = m => {
  const d = JSON.parse(m.data);
  if (d.id && pend.has(d.id)) { pend.get(d.id)(d.result || { error: d.error }); pend.delete(d.id); return; }
  const p = d.params || {};
  if (d.method === "Runtime.exceptionThrown") errors.push(p.exceptionDetails.exception ? p.exceptionDetails.exception.description : p.exceptionDetails.text);
  if (d.method === "Network.requestWillBeSent") reqs.set(p.requestId, { url: p.request.url.replace(/^https?:\/\/[^/]+/, ""), bytes: 0 });
  if (d.method === "Network.loadingFinished") { const r = reqs.get(p.requestId); if (r) r.bytes = p.encodedDataLength; }
};
const send = (method, params = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
await send("Network.enable"); await send("Page.enable"); await send("Runtime.enable"); await send("Network.setCacheDisabled", { cacheDisabled: true });
await send("Emulation.setDeviceMetricsOverride", { width: plan.width || 1440, height: plan.height || 900, deviceScaleFactor: 1, mobile: (plan.width || 1440) < 600 });
await send("Page.navigate", { url: plan.url });
const results = [];
for (const step of plan.steps) {
  if (step.wait) await sleep(step.wait);
  else if (step.eval) {
    const r = await send("Runtime.evaluate", { expression: step.eval, awaitPromise: true, returnByValue: true });
    results.push(r.exceptionDetails ? { error: JSON.stringify(r.exceptionDetails).slice(0, 400) } : r.result ? r.result.value : null);
  } else if (step.shot) {
    const s = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(step.shot, Buffer.from(s.data, "base64"));
  } else if (step.move) await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: step.move[0], y: step.move[1] });
}
console.log(JSON.stringify({ results, errors, requests: [...reqs.values()] }));
ws.close(); ch.kill();
