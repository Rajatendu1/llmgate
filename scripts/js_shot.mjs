// Screenshot helper: boot headless Chrome via CDP, capture the landing page
// before and after clicking the delegation button. Local pixel evidence.
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PAGE = process.argv[2] || 'http://127.0.0.1:8078/';
const OUT1 = process.argv[3] || '/tmp/shot-before.png';
const OUT2 = process.argv[4] || '/tmp/shot-after.png';
const PORT = 9378;
const profile = mkdtempSync(join(tmpdir(), 'js-shot-'));
const chrome = spawn('google-chrome', [
  '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
  '--window-size=900,1400',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, 'about:blank',
], { stdio: ['ignore', 'ignore', 'pipe'] });
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function pageTarget() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find(t => t.type === 'page');
      if (page) return page;
    } catch {}
    await sleep(200);
  }
  throw new Error('chrome never came up');
}
let idc = 0; const pending = new Map(); let ws;
function send(method, params = {}) {
  return new Promise((resolve) => {
    const id = ++idc; pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function main() {
  const target = await pageTarget();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
  };
  await send('Page.enable');
  await send('Page.navigate', { url: PAGE });
  await sleep(5000);
  let shot = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(OUT1, Buffer.from(shot.data, 'base64'));
  await send('Runtime.evaluate', { expression: `document.getElementById('delegate').click()` });
  for (let i = 0; i < 20; i++) {
    await sleep(500);
    const r = await send('Runtime.evaluate', { expression:
      `(document.getElementById('delegate-state')||{}).textContent || ''`, returnByValue: true });
    if (r.result && r.result.value && !/running/.test(r.result.value)) break;
  }
  await sleep(500);
  await send('Runtime.evaluate', { expression:
    `document.getElementById('delegate-result').scrollIntoView({block:'center'})` });
  await sleep(400);
  shot = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(OUT2, Buffer.from(shot.data, 'base64'));
  console.log('shots written', OUT1, OUT2);
}
try { await main(); } catch (e) { console.log('SHOT ERROR: ' + e.message); }
finally { chrome.kill('SIGKILL'); rmSync(profile, { recursive: true, force: true }); }
process.exit(0);
