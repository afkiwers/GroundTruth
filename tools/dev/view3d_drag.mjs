// Does the 3D page (report/view3d.py) turn the way the hand expects? Opens the page in headless Edge, drives it
// over the DevTools protocol and drags 80 px to the right once below the middle of the view (the stack's near
// half) and once above it (the far half); prints how far the grabbed edge moved (read from #cam's transform) and
// whether with the mouse or against it. tests/test_view3d.py runs it where node and Edge are installed.
//
//     node tools/dev/view3d_drag.mjs EDGE.exe PAGE.html PROFILE_DIR      (PROFILE_DIR: a scratch folder for Edge)
//
// Exit code 1 when an edge moved against the mouse.
import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";

const [edge, page, profile] = process.argv.slice(2);
const port = 9337;
const proc = spawn(edge, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`, "--window-size=1200,900", "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let target;
for (let i = 0; i < 50 && !target; i++) {
  try { target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.type === "page"); }
  catch { await sleep(200); }
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r));
let id = 0;
const waiting = new Map();
ws.addEventListener("message", e => { const m = JSON.parse(e.data); if (waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); } });
const send = (method, params = {}) => new Promise(r => { const k = ++id; waiting.set(k, r); ws.send(JSON.stringify({ id: k, method, params })); });

// the viewport set over DevTools: Edge 155 headless ignored --window-size (500 x 320), the near-half drag point
// then lay below the page and nothing received the drag (the test read "moved 0.0 px")
await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: pathToFileURL(page).href });
await sleep(2500);
const measure = async () => JSON.parse((await send("Runtime.evaluate", { expression: `(() => {
  const v = document.getElementById('view').getBoundingClientRect();
  const m = new DOMMatrix(getComputedStyle(document.getElementById('cam')).transform);
  return JSON.stringify({near: m.transformPoint(new DOMPoint(0, 100, 0)).x, far: m.transformPoint(new DOMPoint(0, -100, 0)).x,
                         cx: v.left + v.width / 2, cy: v.top + v.height / 2, h: v.height});
})()` })).result.result.value);
const mouse = (type, x, y) => send("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons: type === "mouseReleased" ? 0 : 1, clickCount: 1 });
async function drag(x, y, dx) {
  await mouse("mousePressed", x, y);
  for (let k = 1; k <= 8; k++) await mouse("mouseMoved", x + dx * k / 8, y);
  await mouse("mouseReleased", x + dx, y);
  await sleep(200);
}
let failed = false;
const g = await measure();
for (const [name, y, key] of [["near half (front)", g.cy + 0.3 * g.h, "near"], ["far half (back)", g.cy - 0.3 * g.h, "far"]]) {
  const a = await measure();
  await drag(g.cx, y, 80);
  const b = await measure();
  console.log(`${name}: drag right 80 px -> the ${key} edge moved ${(b[key] - a[key]).toFixed(1)} px ` +
              `(${b[key] > a[key] ? "with the mouse" : "AGAINST the mouse"})`);
  if (!(b[key] > a[key])) failed = true;
}
ws.close();
proc.kill();
process.exit(failed ? 1 : 0);
