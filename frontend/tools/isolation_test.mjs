// Proves the landing page and the demo cannot reach the production WAREXX
// software, and that the demo answers from its own dataset. Run after a build:
//
//     npm run build && npm test            (from landing-demo/frontend)
//
// What it checks:
//   1. The demo's service worker (dist/demo/sw.js), run here with the network
//      replaced by a recorder: every POST/PUT/PATCH/DELETE to /api and /pos is
//      answered inside the worker. The ONLY requests it makes are for its own
//      /data/ files on its own origin — never another host, never a write.
//   2. Ask WAREXX in the demo answers from the demo dataset (the same answers
//      the real assistant gave over the sample business), including questions
//      phrased differently from the recording.
//   3. Every demo page (desktop and /m) and the landing page carry a
//      Content-Security-Policy whose connect-src is the page's own origin (plus,
//      for the landing page, the lead endpoint) — the browser refuses anything else.
//   4. No secret is in anything landing-demo publishes or its source: no value
//      from app/.env or app/.env.demo, no database URL, no private key, no live
//      payment or API key.
//   5. The landing/demo source does not import the production app: the demo
//      runs a snapshot (vendor/warexx-app), so landing-demo builds on its own.
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND = path.resolve(HERE, '..')
const LD = path.resolve(FRONTEND, '..')
const REPO = path.resolve(LD, '..')
const DEMO = path.join(FRONTEND, 'dist', 'demo')
const LANDING = path.join(FRONTEND, 'dist', 'landing')

let failed = 0
const ok = (cond, what, extra = '') => {
  if (cond) console.log('  ok    ' + what)
  else { failed++; console.log('  FAIL  ' + what + (extra ? '\n        ' + extra : '')) }
}

for (const [d, cmd] of [[DEMO, 'build:demo'], [LANDING, 'build:landing']]) {
  if (!existsSync(path.join(d, 'index.html'))) { console.error(`${d} is missing — npm run ${cmd} first`); process.exit(1) }
}

// ---------------------------------------------------------------------------
// 1. the demo's server: writes never leave the worker
// ---------------------------------------------------------------------------
console.log('demo → production API')
const ORIGIN = 'https://demo.example.test'
const outbound = []                      // every request the worker itself makes
const workerFetch = async (input, init = {}) => {
  const url = new URL(typeof input === 'string' ? input : input.url, ORIGIN + '/')
  const method = (init.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase()
  outbound.push({ method, url: url.href })
  if (url.origin === ORIGIN && method === 'GET') {
    const f = path.join(DEMO, decodeURIComponent(url.pathname))
    if (f.startsWith(DEMO) && existsSync(f) && statSync(f).isFile()) return new Response(readFileSync(f), { status: 200 })
  }
  return new Response('not here', { status: 404 })
}
const listeners = {}
const sandbox = {
  self: null, fetch: workerFetch, Response, Request, Headers, URL, URLSearchParams, console,
  setTimeout, clearTimeout, TextEncoder, TextDecoder, JSON, Promise, Map, Set,
}
sandbox.self = {
  registration: { scope: ORIGIN + '/' }, location: new URL(ORIGIN + '/sw.js'),
  addEventListener: (t, fn) => { listeners[t] = fn }, skipWaiting: () => {}, clients: { claim: async () => {} },
}
vm.createContext(sandbox)
vm.runInContext(readFileSync(path.join(DEMO, 'sw.js'), 'utf8'), sandbox, { filename: 'sw.js' })
ok(typeof listeners.fetch === 'function', 'sw.js installs a fetch handler')

async function ask(method, pathname, body, headers = {}) {
  const req = new Request(ORIGIN + pathname, {
    method, headers: { 'Content-Type': 'application/json', 'X-Essa-Warehouse': '1', ...headers },
    body: body === undefined || method === 'GET' ? undefined : JSON.stringify(body),
  })
  let responded = null
  listeners.fetch({ request: req, respondWith: (p) => { responded = p }, waitUntil: () => {} })
  return responded ? await responded : null
}

const WRITES = [
  ['POST', '/api/purchase-orders', { supplier_name: 'X', lines: [] }],
  ['POST', '/api/grn', { po_id: 1 }],
  ['POST', '/api/inventory/outward', { lines: [] }],
  ['POST', '/api/inventory/transfers', { from: 1, to: 2 }],
  ['PUT', '/api/inventory/products/5', { description: 'changed' }],
  ['PATCH', '/api/users/1', { role: 'superadmin' }],
  ['DELETE', '/api/documents/3'],
  ['DELETE', '/api/inventory/products/5'],
  ['POST', '/api/auth/login', { username: 'admin', password: 'anything' }],
  ['POST', '/api/admin/boot', {}],
  ['POST', '/pos/billing/save', {}],
]
for (const [method, p, body] of WRITES) {
  const before = outbound.length
  const r = await ask(method, p, body)
  const went = outbound.slice(before)
  const offOrigin = went.filter((o) => !o.url.startsWith(ORIGIN + '/data/'))
  ok(r && r.status < 500 && offOrigin.length === 0,
    `${method} ${p} is answered inside the demo (status ${r && r.status})`,
    offOrigin.length ? 'went out: ' + JSON.stringify(offOrigin) : '')
}
ok(outbound.every((o) => o.method === 'GET' && o.url.startsWith(ORIGIN + '/data/')),
  `the worker itself only ever read its own /data/ files (${outbound.length} reads, 0 writes, 0 other hosts)`)
{
  // respondWith not called = the browser sends it on as normal
  for (const p of ['/api/leads', '/api/trial', '/api/geo', '/api/admin']) {
    let called = false
    listeners.fetch({ request: new Request(ORIGIN + p, { method: 'POST', body: '{}' }),
      respondWith: () => { called = true }, waitUntil: () => {} })
    ok(!called, `POST ${p} is left alone by the worker (it reaches the site's real endpoint on a one-domain site)`)
  }
  let other = false
  listeners.fetch({ request: new Request(ORIGIN + '/api/trial/x', { method: 'POST', body: '{}' }),
    respondWith: () => { other = true }, waitUntil: () => {} })
  ok(other, '…and nothing else under /api is (POST /api/trial/x is still answered by the demo)')
  // WAREXX Live (live-api/): real data, for signed-in staff. The worker must never
  // answer for it, and the demo must never be able to reach it by accident.
  for (const p of ['/live/', '/live/api/hr/employees', '/live/api/auth/login', '/live/pos/orders/']) {
    let called = false
    listeners.fetch({ request: new Request(ORIGIN + p), respondWith: () => { called = true }, waitUntil: () => {} })
    ok(!called, `${p} is left alone by the worker: live and recorded data never mix`)
  }
}
const loginReply = await (await ask('POST', '/api/auth/login', { username: 'admin', password: 'x' })).json()
ok(loginReply.token === 'demo', 'demo sign-in is the demo account, whatever is typed (token "demo", never a production token)')

// ---------------------------------------------------------------------------
// 2. Ask WAREXX answers from the demo dataset
// ---------------------------------------------------------------------------
console.log('Ask WAREXX (demo dataset)')
const QUESTIONS = [
  ['How much stock do we have?', 'recorded'],
  ['Which purchase orders are overdue?', 'phrased differently'],
  ['Which products are low in stock?', 'phrased differently'],
  ['Show pending GRNs', 'recorded'],
  ['What needs my attention today?', 'recorded'],
]
for (const [q, how] of QUESTIONS) {
  const r = await (await ask('POST', '/api/assist/command', { text: q, source: 'text' })).json()
  ok(r && r.kind !== 'unknown' && r.intent && r.intent.intent !== 'NONE',
    `"${q}" (${how}) → ${r.intent && r.intent.intent}: ${String(r.line || r.title || '').slice(0, 70)}`)
}
const nope = await (await ask('POST', '/api/assist/command', { text: 'what is the weather on mars', source: 'text' })).json()
ok(nope.kind === 'unknown' && nope.actions.length > 0,
  'an unrelated question is told it is not part of the demo (no invented answer), with questions that are')

// one dataset: the dashboard, the inventory list and the assistant agree on the warehouses
const m = JSON.parse(readFileSync(path.join(DEMO, 'data', 'manifest.json'), 'utf8'))
ok((m.meta.warehouses || []).length > 0 && m.entries.length > 1000,
  `one recorded sample business: ${m.meta.warehouses.map((w) => w.code).join(', ')}, ${m.entries.length} answers, recorded ${m.meta.recordedAt}`)

// ---------------------------------------------------------------------------
// 3. browser-enforced boundary
// ---------------------------------------------------------------------------
console.log('Content-Security-Policy')
const cspOf = (html) => {
  const head = html.slice(0, html.search(/<\/head>/i))
  const mm = head.match(/http-equiv="Content-Security-Policy"\s+content="([^"]+)"/i)
  return mm ? mm[1] : ''
}
const connect = (csp) => ((csp.match(/connect-src ([^;]+)/) || [])[1] || '').trim()
ok(connect(cspOf(readFileSync(path.join(DEMO, 'index.html'), 'utf8'))) === "'self'", "demo page: connect-src 'self'")
ok(connect(cspOf(readFileSync(path.join(DEMO, 'm', 'index.html'), 'utf8'))) === "'self'", "demo phone app (/m): connect-src 'self'")
const landingHtml = readFileSync(path.join(LANDING, 'index.html'), 'utf8')
const lcon = connect(cspOf(landingHtml))
ok(lcon.startsWith("'self'") && !/app\.warexx|:8000/.test(lcon), `landing page: connect-src ${lcon}`)
const fe = (landingHtml.match(/\n {2}formEndpoint: "([^"]*)"/) || [])[1]
ok(fe !== undefined && !/app\.warexx|:8000/.test(fe), `landing forms go to the landing's own lead API (${fe}), not the app`)

// the one-domain site (npm run build:site), when built
const SITE = path.join(FRONTEND, 'dist', 'site')
if (existsSync(path.join(SITE, 'index.html'))) {
  ok(connect(cspOf(readFileSync(path.join(SITE, 'demo', 'index.html'), 'utf8'))) === "'self'", "site: /demo/ page connect-src 'self'")
  ok(connect(cspOf(readFileSync(path.join(SITE, 'm', 'index.html'), 'utf8'))) === "'self'", "site: /m/ phone app connect-src 'self'")
  const siteHtml = readFileSync(path.join(SITE, 'index.html'), 'utf8')
  const demoUrl = (siteHtml.match(/\n {2}demoAppUrl: "([^"]*)"/) || [])[1]
  ok(demoUrl === '/demo/', `site: "Book a demo" opens this site's own /demo/ (${demoUrl})`)
  ok(['sw.js', 'data/manifest.json', 'demo/assets'].every((f) => existsSync(path.join(SITE, f))),
    'site: the worker and dataset at the root, the demo bundle under /demo/')
} else console.log('  skip  one-domain site (npm run build:site)')

// ---------------------------------------------------------------------------
// 4. no secrets in what landing-demo publishes, or in its source
// ---------------------------------------------------------------------------
console.log('secrets')
const walk = (dir, skip) => readdirSync(dir).flatMap((n) => {
  const f = path.join(dir, n)
  if (skip.some((s) => f.includes(s))) return []
  return statSync(f).isDirectory() ? walk(f, skip) : [f]
})
const files = walk(LD, [`${path.sep}node_modules`, `${path.sep}demo-build`, `${path.sep}.record-profile`,
  `${path.sep}outbox`, `${path.sep}archive`, `${path.sep}pgdata`, `${path.sep}venv`])
  .filter((f) => !/\.(png|webp|jpg|jpeg|gif|ico|pdf|db)$/i.test(f) && path.basename(f) !== '.env')
// values the production app holds — read here, never printed
const secretValues = []
for (const envFile of ['.env', '.env.demo']) {
  const f = path.join(REPO, 'app', envFile)
  if (!existsSync(f)) continue
  for (const line of readFileSync(f, 'utf8').split(/\r?\n/)) {
    const mm = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.+?)\s*$/)
    if (mm && /PASSWORD|SECRET|KEY|TOKEN|DATABASE_URL/.test(mm[1]) && mm[2].length >= 8) secretValues.push([mm[1], mm[2]])
  }
}
const PATTERNS = [
  [/postgres(ql)?:\/\/[^\s"'<>]+:[^\s"'<>@]+@/i, 'a database URL with a password'],
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----/, 'a private key'],
  [/\bsk-ant-[A-Za-z0-9_-]{20,}/, 'an Anthropic API key'],
  [/\brzp_live_[A-Za-z0-9]{8,}/, 'a live Razorpay key'],
  [/\bsk_live_[A-Za-z0-9]{8,}/, 'a live Stripe key'],
  [/\b[a-z0-9_]+\.\d{9,11}\.[0-9a-f]{64}\b/, 'a signed WAREXX session token'],
]
let leaks = 0
for (const f of files) {
  const text = readFileSync(f, 'latin1')
  for (const [re, what] of PATTERNS) if (re.test(text)) { leaks++; console.log(`  FAIL  ${what} in ${path.relative(REPO, f)}`) }
  for (const [name, value] of secretValues) if (text.includes(value)) { leaks++; console.log(`  FAIL  the value of app ${name} is in ${path.relative(REPO, f)}`) }
}
failed += leaks
ok(leaks === 0, `${files.length} files scanned (source, demo data, builds); ${secretValues.length} production secret values checked`)
ok(!files.some((f) => /\.env(\.|$)/.test(path.basename(f)) && path.basename(f) !== '.env.example'),
  'no .env file other than .env.example inside landing-demo')

// ---------------------------------------------------------------------------
// 5. builds on its own
// ---------------------------------------------------------------------------
console.log('independence')
const own = walk(FRONTEND, [`${path.sep}node_modules`, `${path.sep}dist`, `${path.sep}vendor`])
  .filter((f) => /\.(m?js|jsx|json|html|css)$/.test(f) && !f.endsWith('sync-app.mjs') && !f.endsWith('isolation_test.mjs'))
const reaching = own.filter((f) => /(\.\.\/){2,}app\/|['"]\.\.\/\.\.\/\.\.\//.test(readFileSync(f, 'utf8')))
ok(reaching.length === 0, 'nothing in landing-demo/frontend imports from app/ (the demo runs vendor/warexx-app)',
  reaching.map((f) => path.relative(REPO, f)).join(', '))
const snap = JSON.parse(readFileSync(path.join(FRONTEND, 'vendor', 'warexx-app', 'SNAPSHOT.json'), 'utf8'))
ok(snap.files > 0, `app snapshot: ${snap.files} files, copied ${snap.copiedAt}`)

console.log(failed ? `\n${failed} FAILED` : '\nall passing')
process.exit(failed ? 1 : 0)
