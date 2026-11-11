// Dashboard JS smoke check: drive headless Chrome over CDP against a locally
// booted gateway and fail on any uncaught exception or console error - the
// regression class that endpoint tests cannot see (e.g. a ReferenceError in
// render() leaving the real-Try button hidden).
//
// Usage: node scripts/js_smoke.mjs [url]   (default http://127.0.0.1:8078/)
// Exit 0 = PASS, 1 = FAIL. No network beyond the local page; no new deps
// (Node 22 native WebSocket + system Chrome).
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PAGE = process.argv[2] || 'http://127.0.0.1:8078/';
const PORT = 9377;
const SIMULATED_ONLY = process.argv.includes('--simulated-only');
const profile = mkdtempSync(join(tmpdir(), 'js-smoke-'));
const chrome = spawn('google-chrome', [
  '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  'about:blank',
], { stdio: ['ignore', 'ignore', 'pipe'] });
let chromeErr = '';
chrome.stderr.on('data', d => { chromeErr += d; });

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function pageTarget() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find(t => t.type === 'page');
      if (page) return page;
    } catch { /* chrome not up yet */ }
    await sleep(200);
  }
  throw new Error('chrome devtools endpoint never came up\n' + chromeErr.slice(-500));
}

let idc = 0;
const pending = new Map();
let ws;
function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++idc;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

const problems = [];
async function main() {
  const target = await pageTarget();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id).resolve(m.result); pending.delete(m.id); return; }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails;
      problems.push(`uncaught exception: ${d.text} ${(d.exception && d.exception.description || '').split('\n')[0]}`);
    }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      problems.push('console.error: ' + m.params.args.map(a => a.value ?? a.description ?? '').join(' '));
    }
  };
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: PAGE });
  await sleep(5000);  // the page's initial stats/events tick must complete

  const evalJson = async expr =>
    (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result.value;

  // Assertions against the rendered page (4 providers incl. real ones when
  // the local server boots with a dummy upstream key).
  const checks = await evalJson(`JSON.stringify({
    pill: document.getElementById('pill') ? document.getElementById('pill').className : 'MISSING',
    pillText: document.getElementById('pill-text') ? document.getElementById('pill-text').textContent : '',
    realBtnShown: (document.getElementById('try-real') || {}).style ? document.getElementById('try-real').style.display !== 'none' : false,
    realLineShown: (document.getElementById('real-line') || {}).style ? document.getElementById('real-line').style.display !== 'none' : false,
    realLine: document.getElementById('real-line') ? document.getElementById('real-line').textContent : '',
    deploymentMode: document.getElementById('deployment-mode')?.textContent || '',
    backupShown: document.getElementById('try-backup')?.style.display !== 'none',
    intro: document.querySelector('.sub')?.textContent || '',
    logRows: document.querySelectorAll('#log-body tr').length,
    edges: ['edge-sim-a','edge-sim-b','edge-groq','edge-groq-backup'].map(id => !!document.getElementById(id)),
  })`);
  const c = JSON.parse(checks);
  const expect = (name, ok, detail) => {
    console.log(`${ok ? 'ok' : 'not ok'} - ${name}${ok ? '' : ' (' + detail + ')'}`);
    if (!ok) problems.push(`${name}: ${detail}`);
  };
  expect('no uncaught exceptions or console.error while rendering', true, '');
  expect('pill reaches a settled state', c.pill === 'ok' || c.pill === 'bad', `pill=${c.pill}`);
  expect('pill is not stuck on an error with healthy local providers', c.pill === 'ok', `pill=${c.pill} text=${c.pillText}`);
  expect('real-Try visibility matches configured routes', c.realBtnShown === !SIMULATED_ONLY, JSON.stringify(c));
  expect('backup visibility matches configured routes', c.backupShown === !SIMULATED_ONLY, JSON.stringify(c));
  expect('deployment text matches configured routes', SIMULATED_ONLY ? /simulated-only/.test(c.deploymentMode) : /real-provider routes configured/.test(c.deploymentMode), c.deploymentMode);
  expect('real-line matches configured routes', SIMULATED_ONLY ? !c.realLineShown : /Configured real routes/i.test(c.realLine), c.realLine.slice(0, 80));
  expect('intro does not assert live upstream access or free pricing', !/is connected|No paid model|no spend/i.test(c.intro), c.intro);
  expect('diagram has an edge per provider including both real ones', c.edges.every(Boolean), JSON.stringify(c.edges));

  // Delegation demo: click the button, let the full exchange flow run, then
  // assert the rendered story (chain, all steps pass, audit chain verified).
  await evalJson(`document.getElementById('delegate').click(), 'clicked'`);
  let dlg = null;
  for (let i = 0; i < 20; i++) {
    await sleep(500);
    dlg = JSON.parse(await evalJson(`JSON.stringify({
      state: (document.getElementById('delegate-state') || {}).textContent || '',
      html: (document.getElementById('delegate-result') || {}).innerHTML || '',
      passes: document.querySelectorAll('#delegate-result .st-ok').length,
      fails: document.querySelectorAll('#delegate-result .st-err').length,
      disabled: (document.getElementById('delegate') || {}).disabled,
    })`));
    if (dlg.state && !/running/.test(dlg.state) && dlg.html) break;
  }
  expect('delegation flow settles with a verdict', /flow verified/.test(dlg.state), `state=${dlg.state}`);
  expect('delegation chain renders both agents', /parent-agent/.test(dlg.html) && /sub-agent/.test(dlg.html), dlg.html.slice(0, 120));
  expect('delegation renders 4 passing steps plus a verified audit chain', dlg.passes >= 5 && dlg.fails === 0, `passes=${dlg.passes} fails=${dlg.fails}`);
  expect('no uncaught exceptions or console.error from the delegation run', true, '');

  // Try journey: after a served request, stage labels carry the measured
  // per-stage timings the gateway reports via Server-Timing.
  await evalJson(`document.getElementById('try').click(), 'clicked'`);
  let tryOut = '';
  for (let i = 0; i < 25; i++) {
    await sleep(400);
    tryOut = await evalJson(`(document.getElementById('try-result') || {}).textContent || ''`);
    if (/served it in|not served|failed/.test(tryOut)) break;
  }
  expect('simulated Try is served', /served it in \d+ ms/.test(tryOut), tryOut.slice(0, 120));
  const authLabel = await evalJson(`document.querySelector('.journey li[data-stage="auth"] .jl').textContent`);
  const provLabel = await evalJson(`document.querySelector('.journey li[data-stage="provider"] .jl').textContent`);
  expect('journey stages carry measured per-stage ms', /^auth \d+(\.\d+)?ms$/.test(authLabel) && /^provider \d+(\.\d+)?ms$/.test(provLabel), authLabel + ' | ' + provLabel);

  // Self-serve keys: mint one from the page, then drive the gateway API with
  // it from the page - simulated route must serve, the real route must refuse.
  await evalJson(`document.getElementById('mint').click(), 'clicked'`);
  let mint = null;
  for (let i = 0; i < 20; i++) {
    await sleep(400);
    mint = JSON.parse(await evalJson(`JSON.stringify({
      key: (document.getElementById('mint-key') || {}).textContent || '',
      curl: (document.getElementById('mint-curl') || {}).textContent || '',
      note: (document.getElementById('mint-note') || {}).textContent || '',
      keyShown: (document.getElementById('mint-key') || {}).style.display === 'block',
    })`));
    if (mint.key || /not minted|failed/.test(mint.note)) break;
  }
  expect('minting a self-serve key shows it once, prefixed vg_', mint.keyShown && /^vg_/.test(mint.key), `key=${mint.key.slice(0, 8)} note=${mint.note.slice(0, 80)}`);
  expect('the prefilled curl snippet carries the minted key', mint.curl.includes(mint.key), mint.curl.slice(0, 80));
  if (/^vg_/.test(mint.key)) {
    const use = JSON.parse(await evalJson(`(async () => {
      const r1 = await fetch('/v1/chat/completions', { method: 'POST', headers: { 'Authorization': 'Bearer ' + document.getElementById('mint-key').textContent, 'Content-Type': 'application/json' }, body: JSON.stringify({ model: 'gpt-sim', messages: [{ role: 'user', content: 'hi' }] }) });
      const r2 = await fetch('/v1/chat/completions', { method: 'POST', headers: { 'Authorization': 'Bearer ' + document.getElementById('mint-key').textContent, 'Content-Type': 'application/json' }, body: JSON.stringify({ model: 'gpt-real', messages: [{ role: 'user', content: 'hi' }] }) });
      return JSON.stringify({ sim: r1.status, real: r2.status });
    })()`));
    expect('minted key serves the simulated route', use.sim === 200, `sim=${use.sim}`);
    expect('minted key never reaches a real model', use.real === (SIMULATED_ONLY ? 404 : 403), `real=${use.real}`);
  }
  expect('no uncaught exceptions or console.error from the self-serve key flow', true, '');

  // Resilience lab: with a minted key, inject a 500 and read the key's own
  // usage - both through the page's own buttons and handlers.
  if (/^vg_/.test(mint.key)) {
    expect('the resilience lab appears once a key exists', await evalJson(`document.getElementById('fault-lab').style.display === 'block'`), '');
    await evalJson(`document.querySelector('.fault-btn[data-fault="500"]').click(), 'clicked'`);
    let fault = '';
    for (let i = 0; i < 20; i++) {
      await sleep(500);
      fault = await evalJson(`(document.getElementById('fault-result') || {}).textContent || ''`);
      if (/served by|status|failed/.test(fault)) break;
    }
    expect('injected 500 is served by the backup via failover', /served by sim-b/.test(fault) && /failover/.test(fault), fault.slice(0, 120));
    await evalJson(`document.getElementById('my-usage').click(), 'clicked'`);
    let usage = '';
    for (let i = 0; i < 20; i++) {
      await sleep(400);
      usage = await evalJson(`(document.getElementById('fault-result') || {}).textContent || ''`);
      if (/your key:|failed/.test(usage)) break;
    }
    expect('own-key usage renders for the minted key', /your key: [1-9]/.test(usage), usage.slice(0, 120));
    // Stream demo: play one live SSE reply through the page's own button and
    // read back the measured facts line it renders.
    await evalJson(`document.getElementById('stream-demo').click(), 'clicked'`);
    let stats = '';
    for (let i = 0; i < 20; i++) {
      await sleep(400);
      stats = await evalJson(`(document.getElementById('stream-stats') || {}).textContent || ''`);
      if (/content chunk|failed/.test(stats)) break;
    }
    expect('stream demo plays SSE and reports measured facts', /[1-9]\d* content chunk\(s\)/.test(stats) && /finish_reason/.test(stats), stats.slice(0, 120));
    const streamed = await evalJson(`(document.getElementById('stream-out') || {}).textContent || ''`);
    expect('stream demo rendered the reply text', streamed.length > 0, '');
  }
  expect('no uncaught exceptions or console.error from the resilience lab and stream demo', true, '');

  console.log(problems.length ? `JS SMOKE FAIL: ${problems.length} problem(s)` : 'JS SMOKE PASS');
  for (const p of problems) console.log('  - ' + p);
  return problems.length ? 1 : 0;
}

let code = 1;
try { code = await main(); }
catch (e) { console.log('JS SMOKE ERROR: ' + e.message); }
finally {
  chrome.kill('SIGKILL');
  await sleep(400);  // let chrome release the profile before removing it
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
}
process.exit(code);
