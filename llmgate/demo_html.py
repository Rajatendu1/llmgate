"""The public demo landing page (GET /). No secrets.

A live dashboard: a request-flow diagram and counters driven by the
gateway's own metering via GET /demo/stats (llmgate/demo_stats.py).

Visual system: "engineered depth" - dark ink, glass hairline panels, teal
for the simulated fleet, amber for the real upstream, motion only where it
explains actual state (request pulses, pipeline journey, exchange arrows).
Inter for prose, JetBrains Mono for data; Google Fonts is the only external
dependency. prefers-reduced-motion gets a static page.
"""

DEMO_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llmgate - public demo</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --ink: #0a0e14;
  --ink-raise: #0d131c;
  --panel: rgba(255,255,255,.035);
  --panel-2: rgba(255,255,255,.06);
  --line: rgba(255,255,255,.09);
  --line-soft: rgba(255,255,255,.055);
  --text: #e7edf6;
  --muted: #96a2b6;
  --faint: #7c8aa0;
  --teal: #5eead4;
  --teal-soft: rgba(94,234,212,.13);
  --amber: #fbbf24;
  --amber-soft: rgba(251,191,36,.12);
  --ok: #34d399;
  --warn: #fbbf24;
  --bad: #f87171;
  --radius: 14px;
  --sans: 'Inter', ui-sans-serif, system-ui, -apple-system, sans-serif;
  --mono: 'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  font-family: var(--sans);
  max-width: 860px; margin: 0 auto; padding: 1.5rem 1.25rem 4rem;
  color: var(--text); line-height: 1.6; background: var(--ink);
  -webkit-font-smoothing: antialiased;
}
/* film grain over everything, barely-there */
body::after {
  content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 50;
  opacity: .05; mix-blend-mode: overlay;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)' opacity='0.6'/%3E%3C/svg%3E");
}
h1 { font-size: clamp(2.1rem, 5.5vw, 3rem); font-weight: 700; letter-spacing: -.03em; margin: 0 0 .9rem; }
h2 {
  font-size: .78rem; font-weight: 600; text-transform: uppercase; letter-spacing: .14em;
  color: var(--muted); margin: 2.6rem 0 .9rem; display: flex; align-items: center; gap: .6rem;
}
h2::before { content: ''; width: 18px; height: 2px; background: var(--teal); border-radius: 1px; flex: none; }
.sub { color: var(--muted); margin-top: 0; max-width: 66ch; font-size: .95rem; }
code, pre { font-family: var(--mono); background: rgba(255,255,255,.05); border: 1px solid var(--line-soft); border-radius: 5px; font-size: .85em; }
code { padding: .08em .4em; }
pre { padding: 1rem 1.15rem; overflow-x: auto; font-size: .8rem; line-height: 1.65; color: #c9d4e4; background: var(--ink-raise); border-radius: 10px; }
a { color: var(--teal); }
em { color: var(--teal); font-style: normal; font-weight: 500; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th { text-align: left; font-size: .68rem; text-transform: uppercase; letter-spacing: .1em; color: var(--faint); font-weight: 600; padding: .5rem .6rem; border-bottom: 1px solid var(--line); }
td { padding: .55rem .6rem; border-bottom: 1px solid var(--line-soft); color: #ccd6e5; }
td .note, .note { color: var(--muted); font-size: .85rem; }

/* ---- hero ---- */
.hero {
  position: relative; overflow: hidden; border: 1px solid var(--line);
  border-radius: 20px; padding: 2.6rem 2rem 2.2rem; margin-bottom: .5rem;
  background:
    linear-gradient(rgba(255,255,255,.028) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,.028) 1px, transparent 1px),
    radial-gradient(ellipse 90% 130% at 85% -30%, rgba(94,234,212,.13), transparent 55%),
    radial-gradient(ellipse 70% 110% at 5% 110%, rgba(129,140,248,.11), transparent 55%),
    var(--ink-raise);
  background-size: 44px 44px, 44px 44px, auto, auto, auto;
}
.hero .aurora {
  position: absolute; inset: -40%; pointer-events: none; opacity: .55;
  background:
    radial-gradient(circle at 28% 38%, rgba(94,234,212,.16), transparent 32%),
    radial-gradient(circle at 68% 62%, rgba(129,140,248,.13), transparent 36%),
    radial-gradient(circle at 82% 22%, rgba(251,191,36,.07), transparent 30%);
  animation: drift 22s ease-in-out infinite alternate;
}
@keyframes drift {
  from { transform: translate3d(-2.5%, -1.5%, 0) scale(1); }
  to   { transform: translate3d(2.5%, 2%, 0) scale(1.06); }
}
.hero > * { position: relative; }
.brand { display: flex; align-items: center; gap: .65rem; margin-bottom: 1.6rem; }
.brand svg { flex: none; }
.brand span { font-family: var(--mono); font-size: .8rem; letter-spacing: .22em; color: var(--faint); text-transform: uppercase; }

/* status pill */
#pill {
  display: inline-flex; align-items: center; gap: .5rem; padding: .45rem .95rem;
  border-radius: 999px; background: var(--panel-2); border: 1px solid var(--line);
  font-family: var(--mono); font-size: .76rem; color: var(--muted);
}
#pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--warn); box-shadow: 0 0 10px var(--warn); }
#pill.ok .dot { background: var(--ok); box-shadow: 0 0 10px var(--ok); }
#pill.bad .dot { background: var(--bad); box-shadow: 0 0 10px var(--bad); }
#pill.ok { color: #b7f4de; border-color: rgba(52,211,153,.3); }
#pill.bad { color: #fecaca; border-color: rgba(248,113,113,.3); }

/* ---- panels ---- */
.panel {
  background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 1.3rem 1.4rem; position: relative;
  box-shadow: 0 18px 40px -30px rgba(0,0,0,.8), inset 0 1px 0 rgba(255,255,255,.04);
}

/* ---- flow diagram ---- */
#flow { width: 100%; height: auto; display: block; }
#flow text { font-family: var(--mono); fill: var(--text); }
.edge { fill: none; stroke: rgba(255,255,255,.14); stroke-width: 1.6; }
.edge.live-sim { stroke: url(#grad-sim); }
.edge.live-real { stroke: url(#grad-real); }
.edge.open { stroke: var(--bad); stroke-dasharray: 4 5; opacity: .8; }
.node { fill: rgba(255,255,255,.045); stroke: rgba(255,255,255,.13); stroke-width: 1; }
.node.gateway { fill: rgba(94,234,212,.07); stroke: rgba(94,234,212,.45); filter: drop-shadow(0 0 14px rgba(94,234,212,.18)); }
.node.real { fill: rgba(251,191,36,.05); stroke: rgba(251,191,36,.4); }
.breaker { font-size: 10px; fill: var(--faint); }
.breaker.closed { fill: var(--ok); }
.breaker.open { fill: var(--bad); }
.breaker.half-open { fill: var(--warn); }
.pulse { fill: var(--teal); filter: drop-shadow(0 0 6px var(--teal)); }
.pulse.real { fill: var(--amber); filter: drop-shadow(0 0 6px var(--amber)); }

/* ---- counters ---- */
#counters { display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); gap: .7rem; margin-top: 1rem; }
.counter {
  background: linear-gradient(180deg, var(--panel-2), rgba(255,255,255,.015));
  border: 1px solid var(--line); border-radius: 10px; padding: .7rem .8rem .6rem;
}
.counter .v {
  font-family: var(--mono); font-weight: 600; font-size: 1.45rem; letter-spacing: -.02em;
  font-variant-numeric: tabular-nums; transition: color .45s, text-shadow .45s;
}
.counter .v.tick { color: var(--teal); text-shadow: 0 0 18px rgba(94,234,212,.5); }
.counter .l { font-size: .64rem; color: var(--faint); text-transform: uppercase; letter-spacing: .1em; margin-top: .15rem; }

/* ---- buttons ---- */
button {
  font-family: var(--sans); font-weight: 500; font-size: .84rem;
  padding: .55rem 1.15rem; border: 1px solid var(--line); border-radius: 999px;
  background: var(--panel-2); color: var(--text); cursor: pointer; margin: 0 .5rem .5rem 0;
  transition: border-color .2s, box-shadow .2s, transform .12s, background .2s;
}
button:hover:not(:disabled) { border-color: rgba(94,234,212,.55); box-shadow: 0 0 0 3px rgba(94,234,212,.1), 0 6px 18px -8px rgba(94,234,212,.35); transform: translateY(-1px); }
button:active:not(:disabled) { transform: translateY(0); }
button:disabled { opacity: .45; cursor: default; }
input[type=text] {
  font-family: var(--mono); font-size: .8rem; color: var(--text);
  background: var(--panel-2); border: 1px solid var(--line); border-radius: 10px;
  padding: .55rem .8rem; width: 100%; box-sizing: border-box; margin: 0 0 .5rem;
  transition: border-color .2s, box-shadow .2s;
}
input[type=text]:focus { outline: none; border-color: rgba(94,234,212,.55); box-shadow: 0 0 0 3px rgba(94,234,212,.12); }
input[type=text]::placeholder { color: var(--dim); }
pre.keyblock {
  font-family: var(--mono); font-size: .76rem; line-height: 1.55; color: var(--text);
  background: var(--panel-2); border: 1px solid var(--line); border-radius: 10px;
  padding: .7rem .85rem; overflow-x: auto; white-space: pre-wrap; word-break: break-all; margin: .4rem 0 .6rem;
}
pre.keyblock .k { color: var(--teal); }
button.primary {
  background: var(--teal); border-color: var(--teal); color: #052e28; font-weight: 600;
  box-shadow: 0 10px 30px -10px rgba(94,234,212,.55);
}
button.primary:hover:not(:disabled) { border-color: var(--teal); box-shadow: 0 0 0 3px rgba(94,234,212,.18), 0 14px 34px -10px rgba(94,234,212,.65); }
button.real-btn { border-color: rgba(251,191,36,.35); color: #fde9bd; }
button.real-btn:hover:not(:disabled) { border-color: rgba(251,191,36,.65); box-shadow: 0 0 0 3px rgba(251,191,36,.1), 0 6px 18px -8px rgba(251,191,36,.4); }

/* ---- pipeline journey (Try result visual) ---- */
.journey {
  list-style: none; display: flex; align-items: flex-start; gap: 0;
  margin: 1.1rem 0 .4rem; padding: 0; font-family: var(--mono);
}
.journey li { flex: 1; min-width: 0; text-align: center; position: relative; padding-top: 1.15rem; }
.journey li::before {  /* connector */
  content: ''; position: absolute; top: 5px; left: -50%; width: 100%; height: 1.5px;
  background: var(--line); transition: background .3s;
}
.journey li:first-child::before { display: none; }
.journey li::after {  /* node dot */
  content: ''; position: absolute; top: 0; left: 50%; transform: translateX(-50%);
  width: 11px; height: 11px; border-radius: 50%;
  background: var(--ink); border: 1.5px solid var(--line);
  transition: border-color .3s, background .3s, box-shadow .3s;
}
.journey li.run::after { border-color: var(--teal); box-shadow: 0 0 12px rgba(94,234,212,.7); animation: jbeat .7s ease-in-out infinite alternate; }
.journey li.run::before { background: linear-gradient(90deg, var(--teal), rgba(94,234,212,.25)); }
.journey li.pass::after { background: var(--teal); border-color: var(--teal); box-shadow: 0 0 8px rgba(94,234,212,.45); }
.journey li.pass::before { background: var(--teal); }
.journey li.fail::after { background: var(--bad); border-color: var(--bad); box-shadow: 0 0 8px rgba(248,113,113,.5); }
.journey li.fail::before { background: var(--bad); }
.journey li .jl { display: block; font-size: .62rem; letter-spacing: .08em; text-transform: uppercase; color: var(--faint); transition: color .3s; }
.journey li.pass .jl, .journey li.run .jl { color: var(--teal); }
.journey li.fail .jl { color: var(--bad); }
@keyframes jbeat { from { transform: translateX(-50%) scale(1); } to { transform: translateX(-50%) scale(1.35); } }

/* ---- delegation sequence ---- */
.seq { display: flex; align-items: stretch; gap: 0; margin: 1rem 0 .4rem; flex-wrap: wrap; }
.seq .actor {
  flex: 1 1 0; min-width: 108px; text-align: center; padding: .65rem .5rem .55rem;
  background: var(--panel-2); border: 1px solid var(--line); border-radius: 10px;
}
.seq .actor .an { font-family: var(--mono); font-size: .8rem; color: var(--text); }
.seq .actor .ar { font-size: .62rem; text-transform: uppercase; letter-spacing: .1em; color: var(--faint); margin-top: .15rem; }
.seq .actor.narrow { border-color: rgba(94,234,212,.4); background: var(--teal-soft); }
.seq .arrow {
  flex: none; align-self: center; padding: 0 .35rem; color: var(--teal);
  font-family: var(--mono); font-size: .95rem; opacity: 0;
  animation: arin .5s ease forwards;
}
.seq .arrow.deny { color: var(--bad); }
@keyframes arin { from { opacity: 0; transform: translateX(-6px); } to { opacity: 1; transform: none; } }
.stepcards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: .6rem; margin: .8rem 0; }
.stepcard { background: var(--panel); border: 1px solid var(--line-soft); border-radius: 10px; padding: .6rem .75rem; }
.stepcard .sn { font-family: var(--mono); font-size: .74rem; color: var(--text); }
.stepcard .sd { font-size: .72rem; color: var(--muted); margin-top: .25rem; line-height: 1.45; }
.stepcard .sv { font-family: var(--mono); font-size: .68rem; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; }
.chain { display: flex; flex-wrap: wrap; align-items: center; gap: .3rem; margin: .5rem 0; }
.chain .badge { font-family: var(--mono); }
.chain .link { color: var(--faint); font-size: .7rem; }

.badge {
  display: inline-block; padding: .12rem .55rem; border-radius: 999px; font-size: .66rem;
  border: 1px solid rgba(94,234,212,.3); background: var(--teal-soft); color: var(--teal);
  font-family: var(--mono); letter-spacing: .03em;
}
.badge.real { color: var(--amber); border-color: rgba(251,191,36,.35); background: var(--amber-soft); }
.st-ok { color: var(--ok); }
.st-lim { color: var(--warn); }
.st-err { color: var(--bad); }

/* ---- log ---- */
.logscroll { position: relative; }
.logwrap { overflow-x: auto; scrollbar-width: thin; scrollbar-color: var(--line) transparent; }
.logwrap::-webkit-scrollbar { height: 6px; }
.logwrap::-webkit-scrollbar-thumb { background: var(--line); border-radius: 3px; }
.logfade { display: none; position: absolute; top: 0; right: 0; bottom: 0; width: 44px; pointer-events: none;
  background: linear-gradient(to right, rgba(10,14,20,0), var(--ink)); }
.logscroll.can-scroll .logfade { display: block; }
#log td { font-family: var(--mono); font-size: .76rem; white-space: nowrap; }

details summary { color: var(--muted); font-size: .85rem; }
details[open] summary { margin-bottom: .3rem; }
details ul { color: var(--muted); font-size: .85rem; }

@media (max-width: 560px) {
  body { padding: 1rem .85rem 3rem; }
  .hero { padding: 1.8rem 1.2rem 1.5rem; }
  .panel { padding: 1rem .95rem; }
  .journey li .jl { font-size: .53rem; letter-spacing: .04em; }
}
h2, .panel { opacity: 0; transform: translateY(14px); transition: opacity .55s ease, transform .55s ease; }
.in h2, h2.in, .panel.in { opacity: 1; transform: none; }
@media (prefers-reduced-motion: reduce) {
  h2, .panel { opacity: 1; transform: none; transition: none; }
  html { scroll-behavior: auto; }
  .hero .aurora { animation: none; }
  .counter .v { transition: none; }
  .journey li::after, .journey li::before, .journey li .jl { transition: none; }
  .journey li.run::after { animation: none; }
  .seq .arrow { animation: none; opacity: 1; }
  button { transition: none; }
}
</style>
</head>
<body>

<header class="hero">
  <div class="aurora"></div>
  <div class="brand">
    <svg width="26" height="26" viewBox="0 0 26 26" fill="none" aria-hidden="true">
      <path d="M3 13 H9" stroke="#5eead4" stroke-width="1.6" stroke-linecap="round"/>
      <path d="M17 13 H23" stroke="#5eead4" stroke-width="1.6" stroke-linecap="round"/>
      <rect x="9.5" y="5.5" width="7" height="15" rx="2" stroke="#5eead4" stroke-width="1.6"/>
      <circle cx="13" cy="13" r="1.6" fill="#5eead4"/>
    </svg>
    <span>llmgate &middot; live demo</span>
  </div>
  <h1>One front door for<br>every model call.</h1>
  <p class="sub">This is a running LLM gateway: it routes, fails over, rate-limits and meters every request, behind an OpenAI-compatible API. The default request uses <em>simulated</em> providers. Real-model controls appear only when this deployment has a matching configured route; configuration does not prove upstream availability or pricing.</p>
  <p id="deployment-mode" class="note">Route availability is shown below after metadata loads.</p>
  <p style="margin-bottom:0"><span id="pill"><span class="dot"></span><span id="pill-text">connecting ...</span></span></p>
</header>

<h2>Try it live</h2>
<div class="panel">
<p style="margin-top:0;margin-bottom:.4rem">
<input id="try-prompt" type="text" maxlength="280" placeholder="Optional: your own prompt for the simulated route (max 280 chars), e.g. Summarize gravity in one sentence">
<button id="try" class="primary" type="button">Send a test request through the gateway</button>
<button id="try-real" class="real-btn" type="button" style="display:none">Send one through the configured real route</button>
<button id="try-backup" class="real-btn" type="button" style="display:none">Send one through the configured backup route</button>
</p>
<ol class="journey" id="journey" aria-label="Pipeline stages each request passes through">
  <li data-stage="auth"><span class="jl">auth</span></li>
  <li data-stage="admission"><span class="jl">admission</span></li>
  <li data-stage="routing"><span class="jl">routing</span></li>
  <li data-stage="provider"><span class="jl">provider</span></li>
  <li data-stage="metering"><span class="jl">metering</span></li>
</ol>
<p id="try-result" class="note" style="margin-bottom:.7rem">Every served request crosses the five stages above; a click sweeps them, reports the measured total latency, and leaves each stage label carrying its measured share of that request (the gateway's Server-Timing header). The sweep itself is illustrative, and a refused request lights nothing: the page says only what the response says.</p>
<p class="note" style="margin-bottom:0">The first button (simulated) sends the prompt above - or a fixed one-liner when it is empty - through the running gateway (auth, admission, routing, failover, metering) to the mock providers. Custom prompts stay on the simulated route; any real-model controls use a fixed one-line prompt. It uses an in-process visitor identity, not a reusable bearer key: max 24 tokens, at most 10 per minute per address, with its own small shared budget, so clicking fast just gets 429s. No spend, nothing outside this demo.</p>
<details style="margin:.5rem 0 0"><summary style="cursor:pointer">budgets and limits for these buttons</summary><p id="try-real-note2" class="note" style="margin:.5rem 0 0;display:none">The real-route button sends a fixed one-line prompt through the same pipeline. The response names the serving provider and upstream model; a successful call uses real provider resources. Provider pricing and quotas depend on the account and are not verified by this page. Both real controls share a tight budget (about one request per 100 seconds across all visitors). A 429 reports a rate or token limit; it is not proof that every upstream is healthy.</p>
<p id="try-backup-note" class="note" style="margin:.5rem 0 0;display:none">The backup button sends the fixed prompt directly to the configured backup alias. A direct backup request is not evidence of failover. Automatic failover depends on the primary response and the gateway retry policy. Both real controls share the tight visitor budget above.</p></details>
</div>

<h2>Use the API yourself</h2>
<div class="panel">
<p class="note" style="margin-top:0">This demo is also an API you can drive. Mint a temporary key and call the gateway from any OpenAI-compatible client: your request crosses the same auth, admission, routing, failover and metering, and lands in the live log below. Keys reach the <em>simulated</em> fleet only and expire after 24 hours. They never grant real-model access; rejection of an unavailable alias does not prove a real route is configured.</p>
<p style="margin:.4rem 0"><button id="mint" class="primary" type="button">Mint a temporary demo key</button>
<button id="mint-copy" type="button" style="display:none">Copy key</button></p>
<pre class="keyblock" id="mint-key" style="display:none"></pre>
<pre class="keyblock" id="mint-curl" style="display:none"></pre>
<div id="mint-sdk" style="display:none">
<p class="note" style="margin:.6rem 0 .3rem">Drop-in SDK quickstart - the official OpenAI SDKs work against this gateway unchanged (verified end to end here with openai-python 3.46.0 and openai-node 7.43.0: completions, streaming, and the error classes all map). Streaming works too: pass <code>stream=True</code> / <code>stream: true</code> and iterate the chunks - the gateway speaks SSE.</p>
<p class="note" style="margin:.4rem 0 .2rem">Python (<code>pip install openai</code>)</p>
<pre class="keyblock" id="mint-py"></pre>
<p class="note" style="margin:.5rem 0 .2rem">Node.js (<code>npm i openai</code>)</p>
<pre class="keyblock" id="mint-node"></pre>
<p style="margin:.5rem 0 .3rem"><button id="stream-demo" type="button">Watch a streamed reply arrive, chunk by chunk</button></p>
<pre class="keyblock" id="stream-out" style="display:none"></pre>
<p id="stream-stats" class="note" style="display:none;margin:.3rem 0 0"></p>
</div>
<p class="note" id="mint-note" style="display:none;margin-bottom:0"></p>
<div id="fault-lab" style="display:none">
<p class="note" style="margin:.7rem 0 .3rem">Resilience lab - your key can inject a fault at the simulated provider (header <code>X-Simulate-Failure</code>) and watch the gateway's real retry, failover, timeout and stream-abort policy run. The fault hits only the first attempt of your one request; retry and failover legs run clean, so what you observe is genuine policy, not a script.</p>
<p style="margin:0 0 .3rem">
<button class="fault-btn" data-fault="429" type="button">Inject 429</button>
<button class="fault-btn" data-fault="500" type="button">Inject 500</button>
<button class="fault-btn" data-fault="slow" type="button">Inject slow (3s)</button>
<button class="fault-btn" data-fault="truncate" type="button">Truncate a stream</button>
<button id="my-usage" type="button">My key's usage</button>
</p>
<p id="fault-result" class="note" style="margin:.2rem 0 0"></p>
</div>
<details style="margin:.4rem 0 0"><summary style="cursor:pointer">limits on minted keys</summary><p class="note" style="margin:.5rem 0 0">Each key: 0.5 requests/s and 20 tokens/s sustained with a small burst, simulated models only, 24-hour expiry. Minting is bounded too: a few keys per hour per address and a fixed daily total across all visitors, enforced in the shared store. The key is shown once and stored only as a SHA-256 digest server-side, so it cannot be recovered - mint a fresh one if you lose it. When the shared store is not configured, keys live only in the process that minted them and the mint response says so.</p></details>
</div>

<h2>Live request flow</h2>
<div class="panel">
<svg id="flow" viewBox="0 0 700 308" role="img" aria-label="Request flow: client to gateway to configured providers; inactive routes are labeled"></svg>
<p class="note" id="real-line" style="display:none;margin:.6rem 0 0"></p>
<div id="counters">
  <div class="counter"><div class="v" id="c-req">-</div><div class="l">requests</div></div>
  <div class="counter"><div class="v" id="c-ok">-</div><div class="l">completed</div></div>
  <div class="counter"><div class="v" id="c-lim">-</div><div class="l">rate limited</div></div>
  <div class="counter"><div class="v" id="c-err">-</div><div class="l">errors</div></div>
  <div class="counter"><div class="v" id="c-tok">-</div><div class="l">tokens</div></div>
  <div class="counter"><div class="v" id="c-cost">-</div><div class="l">est. cost (simulated)</div></div>
</div>
<p style="margin:.9rem 0 .4rem">
<button id="refresh" type="button">Refresh now</button>
<button id="watch" type="button">Watch live for 60s</button>
<span id="watch-state" class="note"></span></p>
<p class="note" style="margin-bottom:0"><span id="metering-note">Live counters from the gateway's own metering.</span>
Counters load once when the page opens; after that nothing polls on its own. Refresh now pulls once; Watch live polls every 5s for up to 60 seconds and only while this tab is visible, then stops - a forgotten tab cannot keep spending the store's daily allowance. The gateway caches each aggregate for a few seconds so viewers on the same instance share one store read, and the edge rule limits each IP to 60 requests/minute; an absolute cap on store spend is the store plan's own hard quota, not something this page can enforce. A pulse on an edge means that provider's completed-request counter went up since the last refresh; these aggregate counters cannot show the exact path of any single request (a request that failed over may count on both providers).</p>
</div>

<h2>Delegation: a scoped sub-agent</h2>
<div class="panel">
<p style="margin-top:0;margin-bottom:.4rem">
<button id="delegate" type="button">Run the delegation exchange</button>
<span id="delegate-state" class="note"></span></p>
<div id="delegate-result"></div>
<p class="note" style="margin-bottom:0">One click runs a complete OAuth 2.0 Token Exchange (RFC 8693) flow inside the gateway and proves its consequences through the real request path: a user token is exchanged down to a parent agent, then narrowed further to a sub-agent that may reach the plain simulated model (<code>gpt-sim</code>) but is REFUSED the expensive one (<code>gpt-sim-pro</code>) with 403 <code>insufficient_scope</code> - the same aliases the Try buttons above use, all simulated, no real model, no spend. A widening attempt by the sub-agent is denied at the exchange, and every grant and denial lands in a hash-chained audit log whose integrity is verified before the result renders. Honest limits: the issuer is embedded in this process (one trust domain - no remote issuer or JWKS); the demo tokens are explicitly unbound bearer tokens and proof-bound (cnf) tokens are rejected rather than downgraded; each run issues and consumes its own tokens, so serverless cold starts change nothing. This demonstrates delegation semantics and least-privilege enforcement; it is not production authorization.</p>
</div>

<h2>Live request log</h2>
<div class="panel">
<div class="logscroll">
<div class="logwrap" id="logwrap">
<table id="log">
<thead><tr><th>time</th><th>key</th><th>served by</th><th>status</th><th>latency</th><th>tokens (p+c)</th><th>est. cost</th><th>attempts</th></tr></thead>
<tbody id="log-body"><tr><td class="note" colspan="8">no requests recorded yet</td></tr></tbody>
</table>
</div>
<div class="logfade"></div>
</div>
<p class="note" style="margin-bottom:0">The newest 20 requests the gateway served, newest first, from its own request log. Prompt and reply content is never logged - token counts only - and keys appear as their public labels, never secrets. A <span class="badge">simulated</span> row was answered by a mock provider; a <span class="badge real">real</span> row by a live upstream. Same refresh cadence as the counters above; the endpoint is <code>GET /demo/events</code>.</p>
</div>

<h2>Endpoints</h2>
<table>
<tr><td><code>GET /healthz</code></td><td>liveness + per-provider breaker state; no key needed</td></tr>
<tr><td><code>GET /demo/stats</code></td><td>the aggregated counters this page polls; no key, no per-key data</td></tr>
<tr><td><code>GET /demo/events</code></td><td>the recent-request feed above; no key, no content, no secrets</td></tr>
<tr><td><code>GET /v1/models</code></td><td>lists the demo model <code>gpt-sim</code>; needs a key</td></tr>
<tr><td><code>POST /v1/chat/completions</code></td><td>OpenAI-shaped chat completion; add <code>"stream": true</code> for SSE. Responses carry a measured per-stage <code>Server-Timing</code> header (auth, admission, routing, provider; metering too on non-stream)</td></tr>
<tr><td><code>GET /metrics</code>, <code>GET /admin/usage</code></td><td>Prometheus metrics and per-key usage; admin key only</td></tr>
</table>
<h2>Try it</h2>
<pre>curl https://llmgate-demo.vercel.app/healthz

curl -H "Authorization: Bearer YOUR_DEMO_KEY" \
     -H "content-type: application/json" \
     -d '{"model":"gpt-sim","messages":[{"role":"user","content":"hello"}]}' \
     https://llmgate-demo.vercel.app/v1/chat/completions</pre>
<p class="note">Run a few completions and watch them pulse through the diagram above. The demo key is shared privately, not published here. Edge protection: 60 requests/minute per IP, then HTTP 429. The metering status above reports whether this deployment uses a shared store or per-instance memory. Shared-store limits coordinate function instances; in-memory limits do not. Circuit-breaker state stays per instance. The cost figure prices simulated usage at the demo's simulated list prices - an estimate, not billing. This is a demo, not a production-ready service. Provider and hosting charges depend on the account and are not verified by this page.</p>
<script>
const $ = id => document.getElementById(id);
const svgNS = 'http://www.w3.org/2000/svg';
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
const fmtCost = c => c === 0 ? '$0' : (c < 0.01 ? '$' + c.toFixed(7).replace(/0+$/, '') : '$' + c.toFixed(2));
const fmtInt = n => n.toLocaleString('en-US');
let prev = null;

/* Two layouts of the same diagram, picked by available width so every node
   stays readable without sideways scrolling on a phone. */
const LAYOUTS = {
  horizontal: {
    minWidth: 560, viewBox: '0 0 700 308',
    nodes: {
      client: { x: 18,  y: 132, w: 100, h: 44, label: ['you'] },
      gate:   { x: 266, y: 132, w: 148, h: 44, label: ['llmgate'] },
      'sim-a': { x: 526, y: 14,  w: 156, h: 60, role: 'primary' },
      'sim-b': { x: 526, y: 86,  w: 156, h: 60, role: 'backup' },
      'groq': { x: 526, y: 158, w: 156, h: 60, role: 'primary', real: true },
      'groq-backup': { x: 526, y: 230, w: 156, h: 60, role: 'backup', real: true },
    },
    edges: {
      'edge-client': 'M118 154 H266',
      'edge-sim-a': 'M414 154 C 470 154, 474 44, 526 44',
      'edge-sim-b': 'M414 154 C 470 154, 474 116, 526 116',
      'edge-groq': 'M414 154 C 470 154, 474 188, 526 188',
      'edge-groq-backup': 'M414 154 C 470 154, 474 260, 526 260',
    },
  },
  vertical: {
    minWidth: 0, viewBox: '0 0 360 500',
    nodes: {
      client: { x: 130, y: 8,   w: 100, h: 44, label: ['you'] },
      gate:   { x: 106, y: 116, w: 148, h: 44, label: ['llmgate'] },
      'sim-a': { x: 98, y: 210, w: 164, h: 60, role: 'primary' },
      'sim-b': { x: 98, y: 282, w: 164, h: 60, role: 'backup' },
      'groq': { x: 98, y: 354, w: 164, h: 60, role: 'primary', real: true },
      'groq-backup': { x: 98, y: 426, w: 164, h: 60, role: 'backup', real: true },
    },
    /* Provider column is one lane (x 98-262); edges travel in side lanes
       (x 34/52 left, x 326/308 right) and enter node sides, so a line never
       crosses a node. */
    edges: {
      'edge-client': 'M180 52 V116',
      'edge-sim-a': 'M180 160 C 130 166, 34 176, 34 240 H98',
      'edge-sim-b': 'M180 160 C 230 166, 326 176, 326 312 H262',
      'edge-groq': 'M180 160 C 120 170, 52 182, 52 384 H98',
      'edge-groq-backup': 'M180 160 C 240 170, 308 182, 308 456 H262',
    },
  },
};

function el(tag, attrs) {
  const e = document.createElementNS(svgNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}

let layoutMode = null;
function drawFlow() {
  const svg = $('flow');
  const wide = svg.clientWidth >= LAYOUTS.horizontal.minWidth;
  const mode = wide ? 'horizontal' : 'vertical';
  if (mode === layoutMode) return;
  layoutMode = mode;
  const L = LAYOUTS[mode];
  svg.setAttribute('viewBox', L.viewBox);
  svg.textContent = '';
  const defs = el('defs', {});
  const g1 = el('linearGradient', { id: 'grad-sim', x1: '0', y1: '0', x2: '1', y2: '0' });
  g1.appendChild(el('stop', { offset: '0', 'stop-color': 'rgba(94,234,212,.1)' }));
  g1.appendChild(el('stop', { offset: '1', 'stop-color': 'rgba(94,234,212,.75)' }));
  const g2 = el('linearGradient', { id: 'grad-real', x1: '0', y1: '0', x2: '1', y2: '0' });
  g2.appendChild(el('stop', { offset: '0', 'stop-color': 'rgba(251,191,36,.1)' }));
  g2.appendChild(el('stop', { offset: '1', 'stop-color': 'rgba(251,191,36,.75)' }));
  defs.appendChild(g1); defs.appendChild(g2); svg.appendChild(defs);
  for (const id in L.edges) {
    const real = /groq/.test(id);
    svg.appendChild(el('path', { id, class: 'edge ' + (real ? 'live-real' : 'live-sim'), d: L.edges[id] }));
  }
  for (const name in L.nodes) {
    const n = L.nodes[name], g = el('g', { id: 'node-' + name });
    g.appendChild(el('rect', { class: 'node' + (name === 'gate' ? ' gateway' : '') + (n.real ? ' real' : ''),
      x: n.x, y: n.y, width: n.w, height: n.h, rx: 10 }));
    if (n.real) g.appendChild(el('text', { x: n.x + n.w - 8, y: n.y + 13, 'text-anchor': 'end',
      'font-size': 8.5, fill: '#fbbf24', 'letter-spacing': '.08em' })).textContent = 'REAL';
    const cx = n.x + n.w / 2;
    if (n.label) {
      g.appendChild(el('text', { x: cx, y: n.y + n.h / 2 + 4.5, 'text-anchor': 'middle', 'font-size': 13 }))
        .textContent = n.label[0];
    } else {
      g.appendChild(el('text', { x: cx, y: n.y + 21, 'text-anchor': 'middle', 'font-size': 12.5 }))
        .textContent = n.real ? name : name + ' \\u00b7 ' + n.role;  // real nodes carry the REAL tag instead of the role
      g.appendChild(el('text', { id: 'breaker-' + name, class: 'breaker', x: cx, y: n.y + 39, 'text-anchor': 'middle' }))
        .textContent = 'breaker: -';
      g.appendChild(el('text', { id: 'price-' + name, x: cx, y: n.y + 55, 'text-anchor': 'middle', 'font-size': 10, fill: '#7c8aa0' }));
    }
    svg.appendChild(g);
  }
  if (prev) render(prev);  // re-apply breaker states and prices after a rebuild
}
drawFlow();
let resizeTimer = null;
addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawFlow, 150); });

function pulse(edgeId, cls) {
  if (!$(edgeId)) return;
  const c = el('circle', { r: 4.5, class: cls || 'pulse' });
  const m = el('animateMotion', { dur: '0.8s', fill: 'freeze' });
  const mp = el('mpath', { href: '#' + edgeId });
  mp.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', '#' + edgeId);
  m.appendChild(mp); c.appendChild(m); $('flow').appendChild(c);
  setTimeout(() => c.remove(), 900);
}

function setNum(id, v, fmt) {
  const e = $(id), s = fmt(v);
  if (e.textContent !== s) {
    e.textContent = s;
    e.classList.add('tick'); setTimeout(() => e.classList.remove('tick'), 500);
  }
}

function render(j) {
  const t = j.totals;
  setNum('c-req', t.requests, fmtInt);
  setNum('c-ok', t.ok, fmtInt);
  setNum('c-lim', t.rate_limited, fmtInt);
  setNum('c-err', t.errors, fmtInt);
  setNum('c-tok', t.prompt_tokens + t.completion_tokens, fmtInt);
  setNum('c-cost', t.cost_usd, fmtCost);
  $('metering-note').textContent = j.metering === 'redis'
    ? 'Live counters from the gateway\\'s own metering, aggregated in a shared store so they hold across function instances.'
    : 'Live counters from the gateway\\'s own metering, aggregated by this function instance only (no shared store), so they reset when the instance recycles.';
  let allClosed = true;
  for (const p of j.providers) {
    const b = $('breaker-' + p.name);
    if (!b) continue;
    b.textContent = 'breaker: ' + p.breaker;
    b.setAttribute('class', 'breaker ' + p.breaker);
    if (p.breaker !== 'closed') allClosed = false;
    $('price-' + p.name).textContent = p.mode === 'real' ? 'pricing not verified' : (p.price ?
      '$' + p.price.prompt_per_mtok + ' / $' + p.price.completion_per_mtok + ' per 1M tok' : '');
    const edge = $('edge-' + p.name);
    if (edge) edge.setAttribute('class', 'edge ' + (/groq/.test(p.name) ? 'live-real' : 'live-sim') + (p.breaker === 'open' ? ' open' : ''));
    if (prev && !reduceMotion && edge) {
      const before = (prev.providers.find(q => q.name === p.name) || {ok: 0}).ok;
      for (let i = 0; i < Math.min(p.ok - before, 4); i++) setTimeout(() => { pulse('edge-client'); pulse('edge-' + p.name, /groq/.test(p.name) ? 'pulse real' : 'pulse'); }, i * 180);
    }
  }
  // Real nodes dim when their provider is not active on this deployment
  // (no upstream key configured), so the diagram never implies a live route
  // that is not there.
  for (const name of ['groq', 'groq-backup']) {
    const g = $('node-' + name);
    if (!g) continue;
    const active = j.providers.some(p => p.name === name);
    g.setAttribute('opacity', active ? '1' : '0.35');
    if (!active) { const b = $('breaker-' + name); if (b) b.textContent = 'not active'; }
  }
  const realProvs = j.providers.filter(p => p.mode === 'real');
  $('deployment-mode').textContent = realProvs.length
    ? 'This deployment has simulated and real-provider routes configured. A configured route is not an upstream health or pricing check.'
    : 'This deployment is simulated-only. No real-provider route is configured.';
  $('real-line').style.display = realProvs.length ? '' : 'none';
  if (realProvs.length) $('real-line').textContent = 'Configured real routes: ' + realProvs.map(p => p.name + ' (' + (p.models || []).join(', ') + ')').join('; ') + '. Provider access and pricing are account-specific.';
  const hasReal = realProvs.some(p => (p.models || []).includes('gpt-real'));
  $('try-real').style.display = hasReal ? '' : 'none';
  $('try-real-note2').style.display = hasReal ? '' : 'none';
  const hasBackup = realProvs.some(p => (p.models || []).includes('gpt-real-backup'));
  $('try-backup').style.display = hasBackup ? '' : 'none';
  $('try-backup-note').style.display = hasBackup ? '' : 'none';
  $('pill').className = allClosed ? 'ok' : 'bad';
  $('pill-text').textContent = allClosed
    ? 'configured breakers closed - metering: ' + (j.metering === 'redis' ? 'shared store' : 'per instance')
    : 'a circuit breaker is not closed';
}

/* No open-ended polling: one load on open, then manual refresh or a bounded
   60-second visible-only watch - a background tab costs zero store reads. */
const WATCH_S = 60, STEP_MS = 5000;
let watchLeft = 0, timer = null;

function renderEvents(j) {
  const body = $('log-body');
  if (!j.ok) { body.innerHTML = '<tr><td class="note" colspan="8">request log unreachable</td></tr>'; return; }
  if (!j.events.length) { body.innerHTML = '<tr><td class="note" colspan="8">no requests recorded yet</td></tr>'; return; }
  const stCls = s => s === 'ok' ? 'st-ok' : (s.startsWith('rate_limited') ? 'st-lim' : 'st-err');
  body.innerHTML = j.events.map(e => {
    const t = new Date(e.ts * 1000).toLocaleTimeString('en-GB');
    const toks = e.prompt_tokens + ' + ' + e.completion_tokens;
    const cost = e.cost_usd == null ? '-' : fmtCost(e.cost_usd);
    const lat = e.latency_ms == null ? '-' : e.latency_ms + ' ms';
    const badge = '<span class="badge' + (e.mode === 'real' ? ' real' : '') + '">' + e.mode + '</span>';
    return '<tr><td>' + t + '</td><td>' + e.key + '</td><td>' + e.provider + ' ' + badge + '</td><td class="' +
      stCls(e.status) + '">' + e.status + '</td><td>' + lat + '</td><td>' + toks + '</td><td>' + cost +
      '</td><td>' + e.attempts + '</td></tr>';
  }).join('');
  updateLogScroll();
}

/* Show the right-edge fade only when the log table actually overflows. */
function updateLogScroll() {
  const w = $('logwrap');
  if (w) w.parentElement.classList.toggle('can-scroll', w.scrollWidth > w.clientWidth + 4);
}
window.addEventListener('resize', updateLogScroll);

async function tick() {
  try {
    const r = await fetch('/demo/stats');
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'degraded');
    render(j); prev = j;
    fetch('/demo/events').then(r => r.json()).then(renderEvents).catch(() => renderEvents({ok: false}));
    if (j.stale) {
      $('pill').className = 'bad';
      $('pill-text').textContent = 'daily snapshot budget reached - showing the last aggregate';
    }
  } catch (e) {
    $('pill').className = 'bad';
    $('pill-text').textContent = 'stats unreachable';
  }
}

function stopWatch(msg) {
  watchLeft = 0;
  if (timer) { clearTimeout(timer); timer = null; }
  $('watch').disabled = false;
  $('watch-state').textContent = msg || '';
}

function watchStep() {
  timer = null;
  if (watchLeft <= 0) { stopWatch('watch ended'); return; }
  if (document.hidden) { timer = setTimeout(watchStep, 1000); return; }  // paused while hidden
  tick();
  watchLeft -= STEP_MS / 1000;
  $('watch-state').textContent = 'watching live - ' + Math.max(watchLeft, 0) + 's left (visible tab only)';
  timer = setTimeout(watchStep, STEP_MS);
}

/* Pipeline journey: light the five stages as the request crosses them. The
   staged timing is illustrative - the only measured number is the total
   latency the gateway reports; a refused request marks the stage that
   refused it (admission for rate limits, provider for upstream failures),
   from the response status. */
const STAGES = ['auth', 'admission', 'routing', 'provider', 'metering'];
function journeyReset() {
  for (const s of STAGES) {
    const li = document.querySelector('.journey li[data-stage="' + s + '"]');
    li.className = '';
    li.querySelector('.jl').textContent = s;
  }
}
async function journeyRun() {
  for (const s of STAGES) {
    const li = document.querySelector('.journey li[data-stage="' + s + '"]');
    li.className = 'run';
    if (!reduceMotion) await new Promise(r => setTimeout(r, 130));
    li.className = 'pass';
  }
}

const fmtTryCost = c => c == null ? '' : ' - est. cost ' + fmtCost(c) + ' (simulated list price)';
const fmtRealCost = c => c == null ? '' : ' - cost ' + fmtCost(c) + ' (free tier)';
async function doTry(url, btn, out, costFmt, prompt) {
  btn.disabled = true;
  journeyReset();
  out.textContent = 'request in flight ...';
  try {
    const opts = { method: 'POST' };
    if (prompt) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify({ prompt: prompt });
    }
    const r = await fetch(url, opts);
    const j = await r.json();
    if (j.ok) {
      journeyRun();
      out.classList.remove('st-err');
      const u = j.usage || {};
      let timingNote = '';
      if (j.timings_ms) {
        /* Measured per-stage shares of THIS request (the gateway's Server-
           Timing header), written onto the stage labels after the sweep. */
        for (const s of STAGES) {
          const v = j.timings_ms[s];
          if (v != null) {
            document.querySelector('.journey li[data-stage="' + s + '"] .jl').textContent =
              s + ' ' + v + 'ms';
          }
        }
        timingNote = ' Stage labels carry this request' + String.fromCharCode(39) + 's measured per-stage time (Server-Timing).';
      }
      out.textContent = 'Your request: ' + j.provider + (j.mode === 'real' ? ' (real model) ' : ' ') +
        (j.upstream_model ? '[' + j.upstream_model + '] ' : '') +
        'served it in ' + j.latency_ms + ' ms' +
        (j.attempts > 1 ? ' after ' + j.attempts + ' attempts (failover)' : ' on the first attempt') +
        ' - ' + (u.prompt_tokens || 0) + ' prompt + ' + (u.completion_tokens || 0) + ' completion tokens' +
        costFmt(j.cost_usd) + (j.text ? ' - reply: "' + j.text + '"' : '') +
        '. The pulsing path is the route this request actually took (from the gateway routing headers).' + timingNote;
      if (!reduceMotion && j.provider && $('edge-' + j.provider)) {
        pulse('edge-client'); pulse('edge-' + j.provider, j.mode === 'real' ? 'pulse real' : 'pulse');
      }
    } else {
      /* Status alone does not prove which stage refused it (a 429 can come
         from the demo budget OR the upstream), so a refusal lights nothing:
         the stages stay unlit and only the response speaks. */
      journeyReset();
      out.classList.add('st-err');
      out.textContent = 'Your request was not served (' + j.status + '): ' + (j.error || 'unknown') +
        ' - that is the gateway doing its job; the counters count it too.';
    }
    tick();
  } catch (e) {
    journeyReset();
    out.classList.add('st-err');
    out.textContent = 'request failed to reach the demo.';
  }
  btn.disabled = false;
}
$('try').addEventListener('click', () => doTry('/demo/try', $('try'), $('try-result'), fmtTryCost, $('try-prompt').value.trim()));
/* Self-serve keys: mint once, show once, prefill a working curl snippet. */
$('mint').addEventListener('click', async () => {
  const btn = $('mint');
  btn.disabled = true;
  try {
    const r = await fetch('/demo/keys', { method: 'POST' });
    const j = await r.json();
    if (j.ok) {
      $('mint-key').textContent = j.key;
      $('mint-key').style.display = 'block';
      const BS = String.fromCharCode(92), NL = String.fromCharCode(10);
      const payload = JSON.stringify({ model: j.models[0], messages: [{ role: 'user', content: 'Hello' }] });
      $('mint-curl').textContent =
        'curl ' + j.base_url + '/chat/completions ' + BS + NL +
        '  -H "Authorization: Bearer ' + j.key + '" ' + BS + NL +
        '  -H "Content-Type: application/json" ' + BS + NL +
        "  -d '" + payload + "'";
      $('mint-curl').style.display = 'block';
      $('mint-py').textContent =
        'from openai import OpenAI' + NL + NL +
        'client = OpenAI(base_url="' + j.base_url + '", api_key="' + j.key + '")' + NL + NL +
        'r = client.chat.completions.create(' + NL +
        '    model="' + j.models[0] + '",' + NL +
        '    messages=[{"role": "user", "content": "Hello"}],' + NL +
        ')' + NL +
        'print(r.choices[0].message.content)';
      $('mint-node').textContent =
        'import OpenAI from "openai";' + NL + NL +
        'const client = new OpenAI({ baseURL: "' + j.base_url + '", apiKey: "' + j.key + '" });' + NL + NL +
        'const r = await client.chat.completions.create({' + NL +
        '  model: "' + j.models[0] + '",' + NL +
        '  messages: [{ role: "user", content: "Hello" }],' + NL +
        '});' + NL +
        'console.log(r.choices[0].message.content);';
      $('mint-sdk').style.display = 'block';
      $('mint-note').textContent = 'Expires in ' + Math.round(j.expires_in_s / 3600) + ' h - ' + j.limits +
        (j.shared_store ? '' : ' - note: shared store is not configured, so this key lives only in the running process.');
      $('mint-note').style.display = 'block';
      $('mint-copy').style.display = '';
      $('fault-lab').style.display = 'block';
      tick();
    } else {
      $('mint-note').textContent = 'Key not minted (' + r.status + '): ' + (j.error || 'unknown') + '.';
      $('mint-note').style.display = 'block';
    }
  } catch (e) {
    $('mint-note').textContent = 'request failed to reach the demo.';
    $('mint-note').style.display = 'block';
  }
  btn.disabled = false;
});
/* Stream demo: play one live SSE reply into the page as the chunks land and
   report only measured facts (first-chunk time, chunk count, total time,
   finish reason) - the same events the SDK snippets above iterate. */
$('stream-demo').addEventListener('click', async () => {
  const btn = $('stream-demo'), out = $('stream-out'), stats = $('stream-stats');
  const key = $('mint-key').textContent;
  btn.disabled = true;
  out.classList.remove('st-err');
  out.textContent = ''; out.style.display = 'block';
  stats.style.display = 'none';
  const t0 = performance.now();
  let first = null, chunks = 0, finish = '-', text = '';
  try {
    const r = await fetch('/v1/chat/completions', { method: 'POST',
      headers: { 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'gpt-sim', stream: true, messages: [{ role: 'user', content: 'Stream a short reply so the chunks are visible.' }] }) });
    if (!r.ok || !r.body) {
      out.textContent = 'stream request failed (' + r.status + ').';
      out.classList.add('st-err');
      btn.disabled = false;
      return;
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const rd = await reader.read();
      if (rd.done) break;
      buf += dec.decode(rd.value, { stream: true });
      const lines = buf.split(String.fromCharCode(10));
      buf = lines.pop();
      for (const ln of lines) {
        if (ln.indexOf('data: ') !== 0) continue;
        const data = ln.slice(6).trim();
        if (data === '[DONE]') continue;
        try {
          const j = JSON.parse(data);
          const ch = j.choices && j.choices[0];
          const d = ch && ch.delta && ch.delta.content;
          if (d) { if (first === null) first = performance.now() - t0; chunks++; text += d; out.textContent = text; }
          if (ch && ch.finish_reason) finish = ch.finish_reason;
        } catch (e) {}
      }
    }
    const total = Math.round(performance.now() - t0);
    stats.textContent = 'measured just now: ' + chunks + ' content chunk(s), first after ' + Math.round(first || 0) +
      ' ms, ' + total + ' ms total, finish_reason ' + finish + ' - the same SSE events the SDK examples above iterate.';
    stats.style.display = 'block';
    tick();
  } catch (e) {
    out.textContent = 'request failed to reach the demo.';
    out.classList.add('st-err');
  }
  btn.disabled = false;
});
/* Resilience lab: run an injected-fault request with the minted key and
   report only what the response proves (status, routing headers, measured
   time, observed stream events). */
async function doFault(kind) {
  const out = $('fault-result');
  const key = $('mint-key').textContent;
  out.classList.remove('st-err');
  out.textContent = 'fault armed: ' + kind + ' - request in flight ...';
  const t0 = performance.now();
  try {
    if (kind === 'truncate') {
      const r = await fetch('/v1/chat/completions', { method: 'POST',
        headers: { 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'X-Simulate-Failure': 'truncate' },
        body: JSON.stringify({ model: 'gpt-sim', stream: true, messages: [{ role: 'user', content: 'hi' }] }) });
      const text = await r.text();
      const ms = Math.round(performance.now() - t0);
      const chunks = (text.match(/^data: /gm) || []).length;
      if (text.indexOf('stream_error') >= 0) {
        out.textContent = 'truncate: the stream cut after ' + chunks + ' event(s) and the gateway closed it with an explicit error event - it did not splice another model into the stream (' + ms + ' ms).';
      } else {
        out.textContent = 'truncate: stream ended without the expected error event (' + r.status + ') - unexpected.';
        out.classList.add('st-err');
      }
      tick();
      return;
    }
    const r = await fetch('/v1/chat/completions', { method: 'POST',
      headers: { 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'X-Simulate-Failure': kind },
      body: JSON.stringify({ model: 'gpt-sim', messages: [{ role: 'user', content: 'hi' }] }) });
    const ms = Math.round(performance.now() - t0);
    const prov = r.headers.get('x-gateway-provider') || '-';
    const tried = r.headers.get('x-gateway-attempts') || '-';
    if (r.status === 200) {
      const note = kind === '429' ? 'the gateway retried in place after the injected rate-limit refusal' :
        kind === '500' ? 'the injected 500 tripped the primary and the backup served - failover' :
        'the provider answered about ' + (ms / 1000).toFixed(1) + ' s late - a short client timeout would have given up; the gateway waited';
      out.textContent = kind + ': status 200, served by ' + prov + ' (' + tried + ' provider(s) engaged, ' + ms + ' ms measured) - ' + note + '. The log below recorded it.';
    } else {
      out.textContent = kind + ': status ' + r.status + ' - the gateway refused rather than serve a doubtful answer (' + ms + ' ms).';
    }
    tick();
  } catch (e) {
    out.textContent = 'request failed to reach the demo.';
    out.classList.add('st-err');
  }
}
for (const b of document.querySelectorAll('.fault-btn')) b.addEventListener('click', () => doFault(b.dataset.fault));
$('my-usage').addEventListener('click', async () => {
  const out = $('fault-result');
  out.classList.remove('st-err');
  try {
    const r = await fetch('/demo/usage', { headers: { 'Authorization': 'Bearer ' + $('mint-key').textContent } });
    const j = await r.json();
    if (j.ok) {
      const u = j.usage || {};
      out.textContent = 'your key: ' + (u.requests || 0) + ' request(s), ' +
        (u.prompt_tokens || 0) + ' prompt + ' + (u.completion_tokens || 0) + ' completion tokens' +
        (u.cost_usd ? ', est. cost $' + Number(u.cost_usd).toFixed(7) + ' (simulated list price)' : '') +
        ' - readable only by whoever holds this key.';
    } else {
      out.textContent = 'usage read failed (' + r.status + '): ' + (j.error || 'unknown');
      out.classList.add('st-err');
    }
  } catch (e) {
    out.textContent = 'request failed to reach the demo.';
    out.classList.add('st-err');
  }
});
$('mint-copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('mint-key').textContent); $('mint-copy').textContent = 'Copied'; }
  catch (e) { $('mint-copy').textContent = 'Copy failed - select the key text'; }
});
$('try-real').addEventListener('click', () => doTry('/demo/try?model=real', $('try-real'), $('try-result'), fmtRealCost));
$('try-backup').addEventListener('click', () => doTry('/demo/try?model=real-backup', $('try-backup'), $('try-result'), fmtRealCost));
/* Delegation demo: POST /demo/delegate runs the full exchange flow server-
   side and returns the steps, audit tail and honest labels to render. */
function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
async function doDelegate() {
  const btn = $('delegate'), out = $('delegate-result'), state = $('delegate-state');
  btn.disabled = true;
  state.textContent = 'running the exchange ...';
  out.innerHTML = '';
  try {
    const r = await fetch('/demo/delegate', { method: 'POST' });
    if (r.status === 404) {
      state.textContent = '';
      out.innerHTML = '<p class="note">The delegation demo is not enabled on this deployment.</p>';
      return;
    }
    const j = await r.json();
    const chain = j.delegation_chain || [];
    const actors = ['<div class="actor"><div class="an">demo-user</div><div class="ar">subject</div></div>'];
    actors.push('<div class="arrow" style="animation-delay:.15s">&rarr;</div>');
    actors.push('<div class="actor"><div class="an">' + esc(chain[0] || 'parent-agent') + '</div><div class="ar">llm:complete + expensive</div></div>');
    actors.push('<div class="arrow" style="animation-delay:.35s">&rarr;</div>');
    actors.push('<div class="actor narrow"><div class="an">' + esc(chain[1] || 'sub-agent') + '</div><div class="ar">narrowed: llm:complete</div></div>');
    actors.push('<div class="arrow deny" style="animation-delay:.55s" title="scope-widening exchange denied">&#10799;</div>');
    actors.push('<div class="actor" style="border-color:rgba(248,113,113,.35)"><div class="an">widening</div><div class="ar">denied: invalid_scope</div></div>');
    const stepcards = (j.steps || []).map(s =>
      '<div class="stepcard"><div class="sv ' + (s.status === 'pass' ? 'st-ok' : 'st-err') + '">' +
      esc(s.status) + '</div><div class="sn">' + esc(s.name) + '</div><div class="sd">' + esc(s.detail) + '</div></div>').join('');
    const audit = (j.audit || []).map(e =>
      '<span class="badge">' + esc(e.event) + '</span>').join('<span class="link">&#8594;</span>');
    const labels = (j.labels || []).map(l => '<li>' + esc(l) + '</li>').join('');
    out.innerHTML =
      '<div class="seq">' + actors.join('') + '</div>' +
      '<p class="note" style="margin:.3rem 0 .2rem">delegation chain: <code>demo-user</code> &rarr; <code>' +
      chain.map(esc).join('</code> &rarr; <code>') + '</code>' +
      ' &nbsp;act as: <code>' + esc(j.sub_agent_identity || '') + '</code></p>' +
      '<div class="stepcards">' + stepcards + '</div>' +
      '<p class="note" style="margin:.4rem 0">exchange audit:</p><div class="chain">' + audit + '</div>' +
      '<p class="note" style="margin:.4rem 0">hash chain verifies: <span class="' + (j.audit_chain_verifies ? 'st-ok' : 'st-err') + '">' +
      (j.audit_chain_verifies ? 'yes' : 'NO') + '</span></p>' +
      '<details><summary style="cursor:pointer">honest limits of this demo</summary>' +
      '<ul style="margin:.3rem 0 0">' + labels + '</ul></details>';
    state.textContent = j.ok ? 'flow verified end to end' : 'flow reported a failing step';
    tick();
  } catch (e) {
    state.textContent = '';
    out.innerHTML = '<p class="note">request failed to reach the demo.</p>';
  }
  btn.disabled = false;
}
$('delegate').addEventListener('click', doDelegate);
$('refresh').addEventListener('click', tick);
$('watch').addEventListener('click', () => {
  $('watch').disabled = true;
  watchLeft = WATCH_S;
  watchStep();
});
/* Scroll-reveal: sections fade in as they enter view; static under reduced
   motion. Above-the-fold sections get .in immediately. */
if (!reduceMotion && 'IntersectionObserver' in window) {
  const io = new IntersectionObserver(es => {
    for (const e of es) if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
  }, { threshold: 0.06 });
  for (const t of document.querySelectorAll('h2, .panel')) io.observe(t);
} else {
  for (const t of document.querySelectorAll('h2, .panel')) t.classList.add('in');
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && watchLeft > 0 && !timer) watchStep(); });
tick();
</script>
</body>
</html>
"""
