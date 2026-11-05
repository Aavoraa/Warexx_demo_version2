/* WAREXX static demo — the "server".
 *
 * The demo is the real WAREXX app with no backend behind it. This service
 * worker answers every request the app makes to /api, /pos and /m from answers
 * recorded off a real server running a sample business (tools/record.mjs wrote
 * data/manifest.json and data/bodies/*). Nothing is sent anywhere; nothing is
 * stored. Writes are acknowledged so the screens behave, and the recorded data
 * comes back on the next read — the demo resets itself on every reload.
 */
const VERSION = 'wx-demo-v4'
const DATA = new URL('./data/', self.registration.scope).href

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()))

let manifestP = null
const manifest = () => (manifestP ||= fetch(DATA + 'manifest.json', { cache: 'no-cache' })
  // no recording yet (or one being written): an empty one, so the app still opens
  .then((r) => (r.ok ? r.json() : { entries: [] })).catch(() => ({ entries: [] }))
  .then((m) => {
    // index: exact key, then path without query, then a path "shape" with the
    // numbers taken out — so /api/documents/7 still finds a document when only
    // /api/documents/3 was recorded.
    const exact = new Map(), byPath = new Map(), byShape = new Map()
    for (const e of m.entries) {
      exact.set(e.key, e)
      push(byPath, `${e.method} ${e.wh} ${e.path}`, e)
      push(byPath, `${e.method} * ${e.path}`, e)
      push(byShape, `${e.method} ${e.wh} ${shape(e.path)}`, e)
      push(byShape, `${e.method} * ${shape(e.path)}`, e)
    }
    return { meta: m.meta || {}, exact, byPath, byShape }
  }))
const push = (map, k, v) => { const a = map.get(k); a ? a.push(v) : map.set(k, [v]) }
const shape = (p) => p.replace(/\/\d+(?=\/|$)/g, '/:n').replace(/\/[0-9a-f]{8,}(?=\/|$)/gi, '/:h')

// The two real endpoints on a site that serves the landing page and the demo
// together (npm run build:site): /api/leads (the landing forms; sends an email)
// and /api/trial (the demo's sign-up / sign-in for a booked slot), plus /api/geo
// (the landing page's visitor country, for prices in their currency). None is
// part of the recording, and nothing of the demo's data goes to any of them.
// /api/admin is the leads admin page (/admin) — it must never get demo answers.
// /api/subscribe and /api/payment/* are the real subscription and HDFC
// SmartGateway endpoints: HDFC sends the customer back to /api/payment/return,
// and on a browser that has opened the demo this worker sees that request first.
const REAL = new Set(['/api/leads', '/api/trial', '/api/geo', '/api/admin', '/api/subscribe'])
const ours = (url) => url.origin === self.location.origin && !REAL.has(url.pathname) &&
  !url.pathname.startsWith('/api/payment/') &&
  (url.pathname.startsWith('/api/') || url.pathname === '/api' || url.pathname.startsWith('/pos'))

// The phone app (/m) is real files — the WAREXX phone page, copied in at build
// (tools/mobile-page.mjs) — not a recording. Its screens have addresses of their
// own (/m/grn/12, /m/purchase-orders), which are all that one page.
const isMobilePage = (url) => url.origin === self.location.origin &&
  (url.pathname === '/m' || url.pathname.startsWith('/m/')) && !/\.[a-z0-9]+$/i.test(url.pathname)

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)
  // a page load re-reads the recording, so newly recorded data shows on reload
  // without waiting for this worker to be replaced
  if (event.request.mode === 'navigate' && url.origin === self.location.origin
      && !url.pathname.startsWith('/pos')) manifestP = null
  if (event.request.mode === 'navigate' && isMobilePage(url)) {
    event.respondWith(fetch('/m/index.html', { cache: 'no-cache' }))
    return
  }
  if (!ours(url)) return                      // the app's own files: straight through
  event.respondWith(answer(event.request, url))
})

// ---- Ask WAREXX (tools/record_mobile.py recorded its answers) ----
// A question is a POST whose meaning is in its body, so it is looked up by a key
// made from the body: the words (normalised as the recorder normalised them) and
// the previous answer's intent — "only in warehouse b" means something different
// after "low stock" than after "pending GRNs" — or, for a tap, the intent itself.
// Spoken words wander ("show me the low stock items please"), so a close-enough
// recorded question answers too. One that is not close to anything recorded is
// told so, with questions that are — never answered with something else.
const CMD = '/api/assist/command'
const norm = (s) => String(s || '').toLowerCase().replace(/[’']/g, '')
  .replace(/[^a-z0-9\- ]+/g, ' ').split(/\s+/).filter(Boolean).join(' ')
  .replace(/^(?:(?:hey|ok|okay) )?warexx /, '').replace(/^please /, '').replace(/ please$/, '')
  .replace(/^show me /, 'show ')
const canon = (i) => {
  const f = Object.entries(i.filters || {}).filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${k}=${v}`).sort().join(';')
  return `${i.intent}|${f}|${i.all ? 1 : 0}`
}
// Filler that carries no meaning in a question about the business: "which
// products are low in stock" asks what "show low stock items" asks.
const STOP = new Set(['the', 'a', 'an', 'me', 'my', 'all', 'please', 'show', 'is', 'are', 'what', 'whats', 'do', 'we', 'have',
  'which', 'in', 'of', 'any', 'there', 'items', 'list', 'currently'])
const words = (t) => new Set(t.split(' ').filter((w) => !STOP.has(w)))
const dice = (a, b) => {
  if (!a.size || !b.size) return 0
  let n = 0; for (const w of a) if (b.has(w)) n++
  return (2 * n) / (a.size + b.size)
}
let askIndexFor = null, askIndex = null
function askKeys(m) {
  if (askIndexFor !== m) {
    askIndexFor = m; askIndex = new Map()
    for (const k of m.exact.keys()) {
      const mm = k.match(/^POST (\S+) \/api\/assist\/command#T:([^:]+):(.*)$/)
      if (!mm) continue
      const key = `${mm[1]} ${mm[2]}`
      if (!askIndex.has(key)) askIndex.set(key, [])
      askIndex.get(key).push({ text: mm[3], words: words(mm[3]), key: k })
    }
  }
  return askIndex
}
async function assistAnswer(m, req, wh) {
  let body = {}
  try { body = await req.clone().json() } catch { /* empty */ }
  const W = wh === '-' ? '1' : wh
  const get = (k) => m.exact.get(`POST ${W} ${CMD}#${k}`)
  let hit = null
  if (body.intent && body.intent.intent) {
    hit = get('I:' + canon(body.intent))
  } else {
    const t = norm(body.text)
    const prev = (body.context && body.context.intent && body.context.intent.intent) || '-'
    // "open the second one" / "receive the first one" point INTO the last answer,
    // whose rows travel in the request (context.items) — so they are answered
    // from those rows, exactly as the server answers them, not from a recording
    // made after some other list.
    const ORD = { first: 0, '1st': 0, second: 1, '2nd': 1, third: 2, '3rd': 2, fourth: 3, '4th': 3, fifth: 4, '5th': 4, last: -1 }
    const pm = t.match(/^(open|show|select|view|pick|take|receive)(?: me)?(?: the)? (first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th)(?: one)?$/)
      || t.match(/^(open|show|receive) (?:it|that|this)$/)
    const rows = (body.context && Array.isArray(body.context.items)) ? body.context.items : []
    if (pm && rows.length) {
      let i = pm[2] ? ORD[pm[2]] : 0
      if (i < 0) i = rows.length - 1
      const it = rows[i]
      const card = (kind, line, extra) => json({ ok: kind === 'open', kind, title: 'Open', line, text: body.text || '',
        items: [], metrics: [], actions: [], total: 0, intent: { intent: 'PICK', filters: {} }, context: body.context, ...extra })
      if (!it) return card('empty', `The last answer had ${rows.length} row${rows.length === 1 ? '' : 's'}.`)
      if (pm[1] === 'receive' && it.kind === 'transfer') {
        const r = get('I:' + canon({ intent: 'RECEIVE_TRANSFER', filters: { transfer_id: it.id } }))
        if (r) { const j = await (await replay(r)).json(); j.text = body.text || ''; return json(j) }
      }
      if (it.open) return card('open', `Opening ${it.code || 'it'}.`, { title: 'Opening', open: it.open })
      return card('empty', 'That one has no screen on the phone — it’s on the desktop app.')
    }
    hit = (prev !== '-' && get(`T:${prev}:${t}`)) || get(`T:-:${t}`)
    if (!hit && t) {
      const idx = askKeys(m), want = words(t)
      let best = null, score = 0
      for (const p of (prev !== '-' ? [prev, '-'] : ['-'])) {
        for (const c of idx.get(`${W} ${p}`) || []) {
          const s = dice(want, c.words) + (p === prev && p !== '-' ? 0.01 : 0)
          if (s > score) { score = s; best = c }
        }
      }
      if (best && score >= 0.8) hit = m.exact.get(best.key)
    }
  }
  if (hit) {
    const r = await (await replay(hit)).json()
    r.text = body.text || ''
    r.source = body.source || (body.text ? 'text' : 'tap')
    return json(r)
  }
  const ov = m.exact.get(`GET ${W} /api/assist/overview`)
  let sugg = ['What needs my attention today?', 'Show pending GRNs', 'Show low stock items', 'Show pending purchase orders']
  try { if (ov) sugg = (await (await replay(ov)).json()).suggestions || sugg } catch { /* defaults */ }
  return json({ ok: false, demo: true, kind: 'unknown', title: 'Not part of this demo',
    line: 'The demo answers a set of questions about its sample business. Try one of these:',
    text: body.text || '', items: [], metrics: [], total: 0,
    actions: sugg.slice(0, 5).map((s) => ({ label: s, ask: s })),
    intent: { intent: 'NONE', filters: {} }, context: body.context || {} })
}

// ---- the Command Center's Ask WAREXX (demo-data/tools/record_command_ask.py) ----
// A question is recorded under its words (norm, as the recorder normalises them);
// a close-enough phrasing answers too. Anything else is told it is not part of
// the demo, with the questions that are — never answered with something else.
const ASK = '/api/command/ask', TRACE = '/api/command/trace'
// Like norm, but letters of every script are kept, with their vowel signs: the
// box listens for Tamil as well as English (record_command_ask.py norm, in step)
const cmdNorm = (s) => String(s || '').toLowerCase().replace(/[’']/g, '')
  .replace(/[^\p{L}\p{M}\p{N}\- ]+/gu, ' ').split(/\s+/).filter(Boolean).join(' ')
  .replace(/^(?:(?:hey|ok|okay) )?warexx /, '').replace(/^please /, '').replace(/ please$/, '')
  .replace(/^show me /, 'show ')
const TAMIL = /[\u0B80-\u0BFF]/
let cmdIndexFor = null, cmdIndex = null
function commandKeys(m) {
  if (cmdIndexFor !== m) {
    cmdIndexFor = m; cmdIndex = []
    for (const k of m.exact.keys()) {
      const mm = k.match(/^POST - \/api\/command\/ask#Q:(.*)$/)
      if (mm) cmdIndex.push({ text: mm[1], words: words(mm[1]), key: k })
    }
  }
  return cmdIndex
}
async function commandAnswer(m, req) {
  let body = {}
  try { body = await req.clone().json() } catch { /* empty */ }
  const t = cmdNorm(body.q)
  let hit = t && m.exact.get(`POST - ${ASK}#Q:${t}`)
  if (!hit && t) {
    const want = words(t)
    let best = null, score = 0
    for (const c of commandKeys(m)) { const s = dice(want, c.words); if (s > score) { score = s; best = c } }
    if (best && score >= 0.8) hit = m.exact.get(best.key)
  }
  if (hit) return replay(hit)
  let sugg = ["Today's total sales", "Today's profit", 'Which products are low in stock?', 'Which floor has the highest sales?']
  const ex = m.exact.get('GET - /api/command/examples')
  try { if (ex) sugg = ((await (await replay(ex)).json()).examples || []).map((e) => e.q).filter(Boolean).slice(0, 6) } catch { /* defaults */ }
  // asked in Tamil: suggest the Tamil questions the demo can answer
  if (TAMIL.test(String(body.q || ''))) {
    const ta = commandKeys(m).map((c) => c.text).filter((x) => TAMIL.test(x))
    if (ta.length) {
      return json({ ok: false, demo: true, title: 'இந்த டெமோவில் இல்லை',
        line: 'இந்த டெமோ அதன் மாதிரி வணிகம் பற்றிய சில கேள்விகளுக்கு பதில் அளிக்கும். இவற்றில் ஒன்றைக் கேளுங்கள்:',
        suggestions: [...new Set(ta)].slice(0, 6) })
    }
  }
  return json({ ok: false, demo: true, title: 'Not part of this demo',
    line: 'The demo answers a set of questions about its sample business. Try one of these:', suggestions: sugg })
}

// ---- product search: the recorded catalogue, filtered as the server filters ----
// One recording of the whole catalogue per warehouse; a search is that list with
// the same four fields matched, so typing "shirt" shows shirts rather than the
// unfiltered first page.
async function productsAnswer(m, url, wh) {
  const q = (url.searchParams.get('q') || '').trim().toLowerCase()
  const status = url.searchParams.get('status') || 'all'
  const base = m.exact.get(`GET ${wh} /api/inventory/products?status=all`) || m.exact.get(`GET 1 /api/inventory/products?status=all`)
  if (!base) return null
  let rows = await (await replay(base)).json()
  if (status === 'pending') rows = rows.filter((p) => !p.detailed)
  if (status === 'detailed') rows = rows.filter((p) => p.detailed)
  rows = rows.filter((p) => [p.description, p.sku, p.barcode, p.hsn].some((f) => String(f || '').toLowerCase().includes(q)))
  const off = +(url.searchParams.get('offset') || 0), lim = +(url.searchParams.get('limit') || 0)
  return json(lim ? rows.slice(off, off + lim) : rows.slice(off))
}

// ---- the Item Locator (tools/record_locator.py recorded its answers) ----
// One recorded answer per SKU and per carton label, and an index of every
// per-piece code -> its product and the piece's own number and status. A piece
// is answered as the real server answers it: its product, plus `unit`.
const LOCATE = '/api/inventory/locate'
let unitsP = null, unitsFor = null
const unitIndex = (m) => {
  if (unitsFor !== m) {
    unitsFor = m
    const e = m.exact.get(`GET - ${LOCATE}/units`)
    unitsP = e ? replay(e).then((r) => r.json()).catch(() => ({})) : Promise.resolve({})
  }
  return unitsP
}
async function locateAnswer(m, url) {
  const raw = (url.searchParams.get('code') || '').trim()
  if (!raw) return json({ detail: 'give a code to look up' }, 400)
  // the demo shows the sample codes as SMPL-; the same items printed as ESSA-
  // (the app's own prefix) are the same items, so both are read
  const norm = (c) => c.trim().toUpperCase().replace(/^ESSA-/, 'SMPL-')
  // a scanned QR carries the code among other fields (E1|66|SMPL-00066|…):
  // the whole text first, then each part of it
  const tries = [raw, ...raw.split(/[|,;\s]+/)].map(norm).filter(Boolean)
  const units = await unitIndex(m)
  for (const c of tries) {
    const hit = m.exact.get(`GET - ${LOCATE}?code=${c}`)
    if (hit) return replay(hit)
    const u = units[c]
    if (u) {
      const p = m.exact.get(`GET - ${LOCATE}?code=${u.sku}`)
      if (p) {
        const body = await replay(p).then((r) => r.json())
        return json({ ...body, code: c, unit: { id: null, code: c, seq: u.seq, status: u.status, printed: null } })
      }
    }
  }
  return json({ detail: `Nothing matches '${raw}'. It is not a product QR, a piece code, `
    + 'a carton label, a barcode or a SKU.' }, 404)
}

async function answer(req, url) {
  const m = await manifest()
  const method = req.method.toUpperCase()
  const wh = req.headers.get('X-Essa-Warehouse') || '-'
  const path = url.pathname
  const key = `${method} ${wh} ${path}${url.search}`
  // The Item Locator answers ONE code, so it is never matched loosely: a code
  // nobody recorded must say "nothing matches", not replay some other item.
  if (method === 'GET' && path === LOCATE) return locateAnswer(m, url)
  if (method === 'POST' && path === CMD) return assistAnswer(m, req, wh)
  // the Command Center's question box (record_command_ask.py)
  if (method === 'POST' && path === ASK) return commandAnswer(m, req)
  // a code is followed only if it was recorded — never some other code's trail
  if (method === 'GET' && path === TRACE) {
    const e = m.exact.get(`GET - ${path}${url.search}`)
    return e ? replay(e) : json({ ok: false, title: 'Not part of this demo',
      line: 'The demo can follow the codes in its sample business — click a product in Leaders, or ask one of the suggested questions.' })
  }
  // a search the recording has no exact answer for: the catalogue, filtered
  if (method === 'GET' && path === '/api/inventory/products' && (url.searchParams.get('q') || '').trim()
      && !m.exact.get(key)) {
    const r = await productsAnswer(m, url, wh)
    if (r) return r
  }
  // a code lookup answers that code or nothing — never a neighbouring product
  if (method === 'GET' && path === '/api/inventory/lookup') {
    const c = (url.searchParams.get('code') || '').trim().toUpperCase().replace(/^ESSA-/, 'SMPL-')
    const e = m.exact.get(`GET - ${path}?code=${c}`)
    return e ? replay(e) : json({ detail: `No product for '${url.searchParams.get('code')}'` }, 404)
  }

  // HR's people: an employee added or edited in the demo is kept for the visit
  // (the recording cannot know them), so the record that was saved is the one
  // shown back — not the nearest recorded person.
  const hrEmp = await hrEmployeeAnswer(m, req, method, path, url)
  if (hrEmp) return hrEmp

  const hit = m.exact.get(key) || m.exact.get(`${method} - ${path}${url.search}`)
    || pick(m.byPath.get(`${method} ${wh} ${path}`), url)
    || pick(m.byPath.get(`${method} * ${path}`), url)
    || pick(m.byShape.get(`${method} ${wh} ${shape(path)}`), url)
    || pick(m.byShape.get(`${method} * ${shape(path)}`), url)
  if (hit) return replay(hit)
  // My profile (user menu): the Sample Admin's account; what the visitor edits
  // is kept by this worker for the visit, like every other save in the demo.
  if (path === '/api/auth/profile') {
    if (method === 'PUT') {
      let b = {}
      try { b = await req.clone().json() } catch { /* empty */ }
      const name = String(b.full_name || '').trim().replace(/\s+/g, ' ')
      if (!name) return json({ detail: 'Your name cannot be empty' }, 400)
      Object.assign(PROFILE, { full_name: name.slice(0, 80), job_title: String(b.job_title || '').trim().slice(0, 80),
        email: String(b.email || '').trim().toLowerCase().slice(0, 254), phone: String(b.phone || '').trim().slice(0, 20) })
      PROFILE.recent = [{ at: new Date().toISOString(), action: 'modify', screen: 'session', outcome: 'ok',
        summary: 'updated their profile (demo — resets when the demo restarts)', warehouse: '' }, ...PROFILE.recent].slice(0, 8)
    }
    return json(PROFILE)
  }
  // Signing in never depends on the recording: whatever is typed, the visitor is
  // the demo's Sample Admin. (A recorded answer, above, wins when there is one.)
  if (AUTH[path]) return json(AUTH[path])

  if (method === 'POST' && (path === '/api/documents/upload' || path === '/api/documents/manual')) {
    return sampleInvoice(m, wh, path)
  }
  if (method === 'GET' || method === 'HEAD') return missingRead(path)
  return acknowledgeWrite(req, path, m.meta)
}

// An invoice upload (or a blank manual entry): there is no server here to read a
// file, and pretending to would be a lie about the visitor's own bill. So the
// answer is one of the sample business's recorded invoices still awaiting review
// (in this warehouse when it has one), in the shape the upload answers with, and
// marked as the demo's — the screen says so instead of "recognised".
async function sampleInvoice(m, wh, path) {
  const pickFrom = (w) => [...m.exact.values()].filter((e) => e.method === 'GET' && e.wh === w && /^\/api\/documents\/\d+$/.test(e.path) && e.body)
  const docs = [...pickFrom(String(wh)), ...pickFrom('-')]
  const parsed = docs.map((e) => { try { return JSON.parse(e.body) } catch { return null } }).filter((j) => j && j.document)
  const sample = parsed.find((j) => j.document.status === 'needs_review') || parsed[0]
  const message = path.endsWith('/manual')
    ? 'This is the demo — new entries are not saved here, so here is a sample invoice to review instead.'
    : 'This is the demo — your file is not read here. Here is a sample invoice as WAREXX extracted it: review it, then Confirm.'
  if (!sample) return json({ detail: message }, 409)
  return json({ ...sample, demo: true, message, supplier_recognised: !!sample.document.supplier_id,
    profile_used: false, lr_filled: [] })
}

// ---- HR employees kept for the visit (demo-data/tools/record_hr.py recorded the rest) ----
// Added people get ids from 9001 up; an edit to a recorded person is laid over
// their recorded record. Both are gone on reload, like every save in the demo.
const HR_ADDED = new Map(), HR_EDITED = new Map()
let hrNextId = 9000
const EMP_RE = /^\/api\/hr\/employees\/(\d+)$/
const recordedBody = async (m, key) => {
  const e = m.exact.get(key)
  if (!e) return null
  try { return await (await replay(e)).json() } catch { return null }
}
async function hrBody(req) {
  try { return await req.clone().json() } catch { return {} }
}
// names for the ids a form sends, from the recorded options
async function hrNamed(m, rec) {
  const o = (await recordedBody(m, 'GET - /api/hr/options')) || {}
  const nameOf = (list, id) => ((list || []).find((x) => String(x.id) === String(id)) || {}).name || ''
  const sal = rec.salary || {}
  return { ...rec,
    department: nameOf(o.departments, rec.department_id), designation: nameOf(o.designations, rec.designation_id),
    shift: nameOf(o.shifts, rec.shift_id), manager: nameOf(o.employees, rec.reporting_to_id) || null,
    gross: Object.values(sal).reduce((a, v) => a + (+v || 0), 0) }
}
async function hrEmployeeAnswer(m, req, method, path, url) {
  if (!path.startsWith('/api/hr/employees')) return null
  if (method === 'POST' && path === '/api/hr/employees') {
    const b = await hrBody(req)
    if (!String(b.name || '').trim()) return json({ detail: 'Give the employee a name' }, 400)
    if (!b.joined_on) return json({ detail: 'When did they join? Put in the joining date' }, 400)
    const id = ++hrNextId
    const today = new Date().toISOString().slice(0, 10)
    const rec = await hrNamed(m, { ...b, id, code: b.code || 'EMP' + String(15 + HR_ADDED.size).padStart(4, '0'),   // the recording holds EMP0001–EMP0014
      status: 'active',
      photo: null, service: '', contracts: [], documents: [], reports: [], leave: [], advances: [],
      history: [{ id: 1, kind: 'Joined', date: b.joined_on, detail: 'Added in the demo', by: 'Sample Admin', at: today }] })
    HR_ADDED.set(id, rec)
    return json(rec)
  }
  const one = path.match(EMP_RE)
  if (one) {
    const id = +one[1]
    const base = HR_ADDED.get(id) || HR_EDITED.get(id) || await recordedBody(m, `GET - ${path}`)
    if (!base) return null
    if (method === 'PUT') {
      const rec = await hrNamed(m, { ...base, ...(await hrBody(req)), id })
      ;(HR_ADDED.has(id) ? HR_ADDED : HR_EDITED).set(id, rec)
      return json(rec)
    }
    if (method === 'GET') return (HR_ADDED.has(id) || HR_EDITED.has(id)) ? json(base) : null
    return null
  }
  if (method === 'GET' && path === '/api/hr/employees' && (HR_ADDED.size || HR_EDITED.size)) {
    const e = m.exact.get(`GET - ${path}${url.search}`) || pick(m.byPath.get(`GET * ${path}`), url)
    if (!e) return null
    let rows = []
    try { rows = await (await replay(e)).json() } catch { return null }
    if (!Array.isArray(rows)) return null
    rows = rows.map((r) => (HR_EDITED.has(r.id) ? { ...r, ...HR_EDITED.get(r.id) } : r))
    const st = url.searchParams.get('status') || ''
    const q = (url.searchParams.get('q') || '').toLowerCase()
    if (!st || st === 'current' || st === 'all') {
      rows = rows.concat([...HR_ADDED.values()].filter((r) => !q || r.name.toLowerCase().includes(q)))
    }
    return json(rows)
  }
  return null
}

// Of several recorded answers for one path, the one whose query shares the
// most parameters with this request (dates move on; the filter usually doesn't).
function pick(list, url) {
  if (!list || !list.length) return null
  if (list.length === 1) return list[0]
  const want = new URLSearchParams(url.search)
  let best = list[0], score = -1
  for (const e of list) {
    const got = new URLSearchParams(e.search || '')
    let s = 0
    for (const [k, v] of want) if (got.get(k) === v) s += 2; else if (got.has(k)) s += 1
    if (s > score) { best = e; score = s }
  }
  return best
}

async function replay(e) {
  const headers = { 'Content-Type': e.type || 'application/json', 'X-Warexx-Demo': '1' }
  if (e.disposition) headers['Content-Disposition'] = e.disposition
  if (e.location) headers.Location = e.location
  if (e.body !== undefined) return new Response(e.body, { status: e.status, headers })
  const r = await fetch(DATA + 'bodies/' + e.file)
  return new Response(await r.arrayBuffer(), { status: e.status, headers })
}

function missingRead(path) {
  if (path.startsWith('/pos') || path === '/m' || path.startsWith('/m/')) {
    return new Response(page('This screen is not part of the demo',
      'Everything else in the menu is — pick another screen to keep exploring.'),
      { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } })
  }
  // A list nobody recorded is an empty list, not an error: the screen shows its
  // own "nothing here yet" instead of a failure.
  return json([], 200)
}

const SESSION = { ok: true, token: 'demo', user: 'superadmin', role: 'superadmin',
  role_label: 'Super Admin', full_name: 'Sample Admin', permissions: {},
  can: { manage_users: true, admin: true, command: true, boss: false } }
const AUTH = {
  '/api/auth/login': SESSION,
  '/api/auth/verify': SESSION,
  '/api/auth/me': { id: 1, username: 'superadmin', role: 'superadmin', role_label: 'Super Admin',
    full_name: 'Sample Admin', active: true, permissions: {} },
  '/api/auth/logout': { ok: true },
}
const PROFILE = { id: 1, username: 'superadmin', role: 'superadmin', role_label: 'Super Admin',
  full_name: 'Sample Admin', job_title: 'Operations head', email: 'admin@sample-business.example', phone: '+91 90000 00000',
  active: true, permissions: {}, created_by: 'system',
  created_at: new Date(Date.now() - 90 * 86400000).toISOString(), last_login_at: new Date().toISOString(),
  recent: [
    { at: new Date(Date.now() - 2 * 60000).toISOString(), action: 'signin', screen: 'session', outcome: 'ok', summary: 'signed in', warehouse: '' },
    { at: new Date(Date.now() - 26 * 3600000).toISOString(), action: 'create', screen: 'grn', outcome: 'ok', summary: 'posted a GRN — 120 units received', warehouse: 'Sample Warehouse A' },
    { at: new Date(Date.now() - 50 * 3600000).toISOString(), action: 'create', screen: 'purchases', outcome: 'ok', summary: 'raised a purchase order to a supplier', warehouse: 'Sample Warehouse A' },
  ] }

let nextId = 90000
async function acknowledgeWrite(req, path, meta) {
  if (path.startsWith('/pos')) {
    // a shop form: back to the page it came from, which still shows the sample
    return new Response(null, { status: 303, headers: { Location: req.referrer || '/pos/' } })
  }
  if (path === '/api/auth/logout') return json({ ok: true })
  let body = {}
  try { body = await req.clone().json() } catch { /* a form or a file */ }
  const id = ++nextId
  // Echo what was sent with an id and an "ok", which is what most screens read
  // back from a save. The message is what a toast shows if the screen surfaces it.
  return json({ ok: true, demo: true, id, ...(body && typeof body === 'object' ? body : {}),
    message: meta.writeMessage || 'Saved in the demo — sample data resets when you reload.' })
}

const json = (v, status = 200) => new Response(JSON.stringify(v), {
  status, headers: { 'Content-Type': 'application/json', 'X-Warexx-Demo': '1' } })

const page = (title, text) => `<!doctype html><meta charset="utf-8"><body style="font:15px/1.6 Inter,system-ui,sans-serif;color:#1A1814;background:#F7F5F1;display:grid;place-items:center;min-height:90vh;margin:0"><div style="text-align:center;max-width:420px;padding:24px"><h2 style="margin:0 0 8px">${title}</h2><p style="color:#6E675E;margin:0">${text}</p></div>`
