// ==========================================================================
//  Finance — the books
//  ------------------------------------------------------------------------
//  Twenty-five screens over one engine (backend services/finance.py): every
//  rupee is a voucher of balanced debit and credit lines, and every statement
//  here — daybook, trial balance, P&L, balance sheet, BRS — is a sum over them.
//
//  What the warehouse and the shop already record (a posted GRN, a supplier
//  payment, a debit note, a day's till sales) is posted into the books by the
//  server on its own, so nothing on these screens asks for it twice. Those
//  vouchers show here read-only, marked "auto".
//
//  Every screen is drawn from the same few parts so they read as one module:
//  FinPage (the page band + a filter column + the work area), FinSide (the
//  filter column), Sel / CheckList (pickers), and the T-statement for the
//  accounts. Dates are typed and shown DD-MM-YYYY and sent ISO, as everywhere.
// ==========================================================================
import React, { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import { api } from './api.js'
import { Loading } from './Loader.jsx'
import './hr.css'
// The page band, fields, pickers, dialogs and pills every Finance screen is drawn
// from — WAREXX's own pieces (hr-kit.jsx), shared with HR.
import {
  FinPage, F, Sel, CheckList, DateRange, Empty, Modal, Badge, useRun, downloadCsv, errText,
  monthStart, todayIso,
} from './hr-kit.jsx'
import { Section, Stat, DateField, SearchBox, money, fmtDate, Icon } from './App.jsx'

// ---------------------------------------------------------------- helpers
const r2 = (v) => Math.round((+v || 0) * 100) / 100
const amt = (v) => (r2(v) ? money(v) : '')
// a signed balance as the books write it: 1,200.00 Dr / 450.00 Cr
const drcr = (v) => (r2(v) ? `${money(Math.abs(v))} ${v >= 0 ? 'Dr' : 'Cr'}` : '—')

function fyStart() {
  const d = new Date()
  const y = d.getMonth() >= 3 ? d.getFullYear() : d.getFullYear() - 1
  return `${y}-04-01`
}

// The options every screen's dropdowns read, fetched once and shared.
let optCache = null
let optWaiters = []
function loadOptions(force) {
  if (optCache && !force) return Promise.resolve(optCache)
  return api.finGet('options').then((o) => { optCache = o; optWaiters.forEach((f) => f(o)); return o })
}
function useFinOptions() {
  const [o, setO] = useState(optCache)
  useEffect(() => {
    let live = true
    const take = (x) => { if (live) setO(x) }
    optWaiters.push(take)
    loadOptions().then(take).catch(() => {})
    return () => { live = false; optWaiters = optWaiters.filter((f) => f !== take) }
  }, [])
  return [o || { groups: [], filters: [], tags: [], vtypes: [], modes: [], businesses: [],
    locations: [], banks: [], cash: [], transports: [], agents: [], petty_groups: [] },
  () => loadOptions(true)]
}

// The ledgers, for the many pickers that need one. Re-read on demand.
function useLedgers(params) {
  const [rows, setRows] = useState([])
  const key = JSON.stringify(params || {})
  const reload = useCallback(() => api.finGet('ledgers', params || {}).then(setRows).catch(() => setRows([])),
    [key]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { reload() }, [reload])
  return [rows, reload]
}











// ==========================================================================
//  Manage Ledger
// ==========================================================================
const BLANK_LEDGER = { name: '', alias: '', group: '', business_id: '', location: '', gstin: '', pan: '',
  state: '', address: '', phone: '', email: '', bank_account: '', ifsc: '', billwise: false,
  credit_days: '', tds_applicable: false, tds_section: '', tds_rate: '', tags: [], opening_balance: 0 }

function LedgerForm({ init, onClose, onSaved, toast, lockGroup }) {
  const [o] = useFinOptions()
  const [f, setF] = useState(() => {
    const x = { ...BLANK_LEDGER, ...(init || {}) }
    const ob = +x.opening_balance || 0
    return { ...x, ob_amt: Math.abs(ob) || '', ob_side: ob < 0 ? 'Cr' : 'Dr' }
  })
  const [busy, run] = useRun(toast)
  const set = (k) => (v) => setF((p) => ({ ...p, [k]: v?.target ? (v.target.type === 'checkbox' ? v.target.checked : v.target.value) : v }))
  const save = async () => {
    const body = { ...f, opening_balance: (f.ob_side === 'Cr' ? -1 : 1) * (+f.ob_amt || 0),
      business_id: f.business_id ? +f.business_id : null,
      credit_days: f.credit_days === '' ? null : +f.credit_days,
      tds_rate: f.tds_rate === '' ? null : +f.tds_rate }
    const r = await run(() => (init?.id ? api.finSend('PUT', `ledgers/${init.id}`, body) : api.finSend('POST', 'ledgers', body)),
      `✓ Ledger ${f.name} saved`)
    if (r) { loadOptions(true); onSaved(r) }
  }
  const tagOn = (k) => (f.tags || []).includes(k)
  return (
    <Modal title={init?.id ? `Edit ledger — ${init.name}` : 'Create ledger'} onClose={onClose} wide
      foot={<>
        {init?.system && <span className="small muted">A system ledger: the auto-postings use it, so its type is fixed.</span>}
        <div className="spacer" />
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !f.name || !f.group} onClick={save}>
          {busy ? 'Saving…' : 'Save ledger'}</button></>}>
      <div className="fin-form">
        <h5>Ledger</h5>
        <F label="Name" req><input autoFocus value={f.name} onChange={set('name')} placeholder="e.g. HDFC Current A/c" /></F>
        <F label="Alias"><input value={f.alias || ''} onChange={set('alias')} /></F>
        <F label="Type" req><Sel value={f.group} onChange={set('group')} placeholder="Choose a type"
          disabled={init?.system || lockGroup} options={o.groups.map((g) => g.name)} /></F>
        <F label="Company"><Sel value={f.business_id || ''} onChange={set('business_id')} placeholder="All companies"
          options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <F label="Location"><Sel value={f.location || ''} onChange={set('location')} placeholder="Every location"
          options={o.locations} /></F>

        <h5>Tax</h5>
        <F label="GSTIN"><input value={f.gstin || ''} onChange={set('gstin')} maxLength={15} /></F>
        <F label="PAN"><input value={f.pan || ''} onChange={set('pan')} maxLength={10} /></F>
        <F label="State"><input value={f.state || ''} onChange={set('state')} /></F>
        <F label="TDS applicable"><label className="fin-check"><input type="checkbox" checked={!!f.tds_applicable}
          onChange={set('tds_applicable')} /> Deduct TDS when paying this ledger</label></F>
        {f.tds_applicable && <>
          <F label="TDS section"><input value={f.tds_section || ''} onChange={set('tds_section')} placeholder="194C" /></F>
          <F label="TDS rate %"><input type="number" value={f.tds_rate ?? ''} onChange={set('tds_rate')} /></F>
        </>}

        <h5>Bank &amp; contact</h5>
        <F label="Bank account no"><input value={f.bank_account || ''} onChange={set('bank_account')} /></F>
        <F label="IFSC"><input value={f.ifsc || ''} onChange={set('ifsc')} /></F>
        <F label="Phone"><input value={f.phone || ''} onChange={set('phone')} /></F>
        <F label="Email"><input value={f.email || ''} onChange={set('email')} /></F>
        <F label="Address" style={{ gridColumn: '1 / -1' }}><textarea rows={2} value={f.address || ''} onChange={set('address')} /></F>

        <h5>Behaviour</h5>
        <F label="Bill-wise"><label className="fin-check"><input type="checkbox" checked={!!f.billwise}
          onChange={set('billwise')} /> Settle bill by bill</label></F>
        <F label="Credit days"><input type="number" value={f.credit_days ?? ''} onChange={set('credit_days')} /></F>
        <F label="Opening balance">
          <div className="fin-inline">
            <input type="number" value={f.ob_amt} onChange={set('ob_amt')} placeholder="0.00" />
            <select value={f.ob_side} onChange={set('ob_side')} style={{ width: 70 }}><option>Dr</option><option>Cr</option></select>
          </div></F>
        <F label="Flags" style={{ gridColumn: '1 / -1' }}>
          <div className="fin-tags">
            {o.tags.map((t) => (
              <label key={t.key} className={'fin-tag' + (tagOn(t.key) ? ' on' : '')}>
                <input type="checkbox" checked={tagOn(t.key)}
                  onChange={() => set('tags')(tagOn(t.key) ? f.tags.filter((x) => x !== t.key) : [...(f.tags || []), t.key])} />
                {t.label}</label>
            ))}
          </div></F>
      </div>
    </Modal>
  )
}

function StatementModal({ ledger, onClose }) {
  const [from, setFrom] = useState(fyStart())
  const [to, setTo] = useState(todayIso())
  const [st, setSt] = useState(null)
  useEffect(() => {
    api.finGet(`ledgers/${ledger.id || ledger.ledger_id}/statement`, { date_from: from, date_to: to })
      .then(setSt).catch(() => setSt(null))
  }, [ledger, from, to])
  return (
    <Modal title={`Ledger statement — ${ledger.name || ledger.ledger || ledger.party || ledger.loan}`} onClose={onClose} wide
      foot={<><div className="spacer" />
        {st && <button className="btn" onClick={() => downloadCsv(`statement-${st.ledger.name}`,
          [['date', 'Date'], ['voucher_no', 'Voucher'], ['vtype', 'Type'], ['narration', 'Narration'],
            ['debit', 'Debit'], ['credit', 'Credit'], ['balance', 'Balance']], st.rows)}>
          <Icon name="spreadsheet" size={14} /> Export</button>}
        <button className="btn primary" onClick={onClose}>Close</button></>}>
      <div className="fin-bar">
        <DateRange from={from} to={to} setFrom={setFrom} setTo={setTo} />
        {st && <div className="fin-stats">
          <Stat label="Opening" value={drcr(st.opening)} />
          <Stat label="Closing" value={drcr(st.closing)} accent="money" />
        </div>}
      </div>
      {!st ? <Loading>Reading the ledger…</Loading> : st.rows.length === 0 ? <Empty>No entries in this period.</Empty> : (
        <div className="tablewrap"><table className="items fin-table">
          <thead><tr><th>Date</th><th>Voucher</th><th>Type</th><th>Narration</th>
            <th className="r">Debit</th><th className="r">Credit</th><th className="r">Balance</th></tr></thead>
          <tbody>{st.rows.map((r, i) => (
            <tr key={i}><td>{fmtDate(r.date)}</td><td className="mono">{r.voucher_no}</td><td>{r.vtype}</td>
              <td className="wrap">{r.narration}</td><td className="r">{amt(r.debit)}</td>
              <td className="r">{amt(r.credit)}</td><td className="r b">{drcr(r.balance)}</td></tr>
          ))}</tbody>
        </table></div>
      )}
    </Modal>
  )
}

function ManageLedger({ toast }) {
  const [o] = useFinOptions()
  const [group, setGroup] = useState('')
  const [flt, setFlt] = useState('')
  const [name, setName] = useState('')
  const [rows, setRows] = useState(null)
  const [edit, setEdit] = useState(null)
  const [stmt, setStmt] = useState(null)
  const load = useCallback(() => {
    api.finGet('ledgers', { group, filter: flt, name }).then(setRows).catch((e) => { toast(errText(e), 'err'); setRows([]) })
  }, [group, flt, name, toast])
  useEffect(() => { const t = setTimeout(load, 250); return () => clearTimeout(t) }, [load])
  const del = async (L) => {
    if (!window.confirm(`Delete the ledger ${L.name}? A ledger with entries is kept and hidden instead.`)) return
    try {
      const r = await api.finSend('DELETE', `ledgers/${L.id}`)
      toast(r.deactivated ? `${L.name} has ${r.lines} entries — hidden from lists, kept in the books` : `✓ ${L.name} deleted`, 'ok')
      load()
    } catch (e) { toast(errText(e), 'err') }
  }
  const cols = [['name', 'Name'], ['group', 'Type'], ['gstin', 'GSTIN'], ['pan', 'PAN'],
    [(r) => (r.billwise ? 'Yes' : 'No'), 'Billwise'], ['opening_balance', 'Opening'], ['balance', 'Balance']]
  return (
    <FinPage title="Manage Ledger" sub={rows ? `${rows.length} ledger${rows.length === 1 ? '' : 's'}` : ''}
      actions={<>
        <button className="btn primary" onClick={() => setEdit({})}><Icon name="plus" size={14} /> Create Ledger</button>
        <button className="btn" onClick={load}><Icon name="search" size={14} /> Search</button>
        <button className="btn" disabled={!rows?.length} onClick={() => downloadCsv('ledgers', cols, rows)}>
          <Icon name="spreadsheet" size={14} /> Export</button></>}>
      <div className="fin-toolbar">
        <F label="Type"><Sel value={group} onChange={setGroup} placeholder="All types" options={o.groups.map((g) => g.name)} /></F>
        <F label="Filter"><Sel value={flt} onChange={setFlt} placeholder="No filter"
          options={o.filters.map((x) => ({ value: x.key, label: x.label }))} /></F>
        <F label="Name" style={{ flex: 1, maxWidth: 360 }}><input value={name} onChange={(e) => setName(e.target.value)}
          placeholder="Enter ledger name" /></F>
      </div>
      {rows === null ? <Loading>Reading the ledgers…</Loading> : rows.length === 0 ? (
        <Empty>No ledger matches. <button className="btn" onClick={() => setEdit({ group })}>Create one</button></Empty>
      ) : (
        <div className="tablewrap fin-scroll"><table className="items fin-table">
          <thead><tr><th>Name</th><th>Type</th><th>GSTIN</th><th>PAN</th><th>Billwise</th><th>TDS</th>
            <th className="r">Opening</th><th className="r">Balance</th><th></th></tr></thead>
          <tbody>{rows.map((L) => (
            <tr key={L.id} className="clickable" onDoubleClick={() => setEdit(L)}>
              <td><b>{L.name}</b>{L.system && <Badge tone="posted" title="Used by the auto-postings">system</Badge>}
                {L.location && <div className="small muted">{L.location}</div>}</td>
              <td>{L.group}</td>
              <td className="mono">{L.gstin || <span className="muted">—</span>}</td>
              <td className="mono">{L.pan || <span className="muted">—</span>}</td>
              <td>{L.billwise ? 'Yes' : 'No'}</td>
              <td>{L.tds_applicable ? `${L.tds_section || 'Yes'}${L.tds_rate ? ` · ${L.tds_rate}%` : ''}` : ''}</td>
              <td className="r">{drcr(L.opening_balance)}</td>
              <td className="r b">{drcr(L.balance)}</td>
              <td className="acts">
                <button className="btn sm" onClick={() => setStmt(L)} title="Statement">Statement</button>
                <button className="btn sm" onClick={() => setEdit(L)} title="Edit"><Icon name="pencil" size={13} /></button>
                {!L.system && <button className="btn sm danger" onClick={() => del(L)} title="Delete"><Icon name="trash" size={13} /></button>}
              </td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
      {edit && <LedgerForm init={edit.id ? edit : { ...edit }} toast={toast} onClose={() => setEdit(null)}
        onSaved={() => { setEdit(null); load() }} />}
      {stmt && <StatementModal ledger={stmt} onClose={() => setStmt(null)} />}
    </FinPage>
  )
}

// ==========================================================================
//  Voucher Entry
// ==========================================================================
const blankLine = () => ({ ledger_id: '', debit: '', credit: '', narration: '', bill_ref: '' })

function VoucherForm({ init, onClose, onSaved, toast, presetType }) {
  const [o] = useFinOptions()
  const [ledgers] = useLedgers()
  const [h, setH] = useState(() => ({ vtype: presetType || 'Journal', date: todayIso(), mode: '', location: '',
    business_id: '', ref_no: '', cheque_no: '', cheque_date: '', narration: '', ...(init || {}) }))
  const [lines, setLines] = useState(() => (init?.lines?.length ? init.lines.map((l) => ({ ...l,
    debit: l.debit || '', credit: l.credit || '' })) : [blankLine(), blankLine()]))
  const [busy, run] = useRun(toast)
  const dr = r2(lines.reduce((a, l) => a + (+l.debit || 0), 0))
  const cr = r2(lines.reduce((a, l) => a + (+l.credit || 0), 0))
  const diff = r2(dr - cr)
  const setLine = (i, k, v) => setLines((ls) => ls.map((l, j) => (j === i ? { ...l, [k]: v,
    ...(k === 'debit' && v ? { credit: '' } : k === 'credit' && v ? { debit: '' } : {}) } : l)))
  const balanceLast = () => {
    if (!diff) return
    setLines((ls) => {
      const i = ls.findIndex((l) => l.ledger_id && !+l.debit && !+l.credit)
      const at = i >= 0 ? i : ls.length
      const next = i >= 0 ? [...ls] : [...ls, blankLine()]
      next[at] = { ...next[at], ...(diff > 0 ? { credit: diff, debit: '' } : { debit: -diff, credit: '' }) }
      return next
    })
  }
  const save = async () => {
    const body = { ...h, business_id: h.business_id ? +h.business_id : null,
      lines: lines.filter((l) => l.ledger_id && (+l.debit || +l.credit))
        .map((l) => ({ ledger_id: +l.ledger_id, debit: +l.debit || 0, credit: +l.credit || 0,
          narration: l.narration || null, bill_ref: l.bill_ref || null })) }
    const r = await run(() => (init?.id ? api.finSend('PUT', `vouchers/${init.id}`, body) : api.finSend('POST', 'vouchers', body)),
      (v) => `✓ ${v.vtype} ${v.voucher_no} saved`)
    if (r) onSaved(r)
  }
  const readOnly = init?.id && !init.editable
  return (
    <Modal title={init?.id ? `${init.vtype} ${init.voucher_no}` : 'New voucher'} onClose={onClose} wide
      foot={<>
        {readOnly ? <span className="small muted">Posted from the warehouse — correct it where it was raised.</span>
          : <span className={'fin-diff' + (diff ? ' bad' : ' ok')}>
            Debit ₹{money(dr)} · Credit ₹{money(cr)} {diff ? `· difference ₹${money(Math.abs(diff))}` : dr ? '· balanced ✓' : '· enter the lines'}</span>}
        <div className="spacer" />
        {!readOnly && diff !== 0 && <button className="btn" onClick={balanceLast}>Balance it</button>}
        <button className="btn" onClick={onClose}>{readOnly ? 'Close' : 'Cancel'}</button>
        {!readOnly && <button className="btn primary" disabled={busy || diff !== 0 || dr === 0} onClick={save}>
          {busy ? 'Saving…' : 'Save voucher'}</button>}</>}>
      <div className="fin-form four">
        <F label="Type" req><Sel value={h.vtype} allowBlank={false} disabled={readOnly}
          onChange={(v) => setH({ ...h, vtype: v })} options={o.vtypes} /></F>
        <DateField label="Date" required value={h.date} onChange={(v) => setH({ ...h, date: v })} />
        <F label="Mode"><Sel value={h.mode || ''} onChange={(v) => setH({ ...h, mode: v })} placeholder="—" options={o.modes} /></F>
        <F label="Location"><Sel value={h.location || ''} onChange={(v) => setH({ ...h, location: v })} placeholder="—" options={o.locations} /></F>
        <F label="Company"><Sel value={h.business_id || ''} onChange={(v) => setH({ ...h, business_id: v })} placeholder="Default"
          options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <F label="Ref no"><input value={h.ref_no || ''} onChange={(e) => setH({ ...h, ref_no: e.target.value })} /></F>
        <F label="Cheque no"><input value={h.cheque_no || ''} onChange={(e) => setH({ ...h, cheque_no: e.target.value })} /></F>
        <DateField label="Cheque date" value={h.cheque_date || ''} onChange={(v) => setH({ ...h, cheque_date: v })} />
      </div>
      <div className="tablewrap"><table className="items fin-table fin-entry">
        <thead><tr><th style={{ width: 34 }}>#</th><th>Ledger</th><th className="r" style={{ width: 130 }}>Debit</th>
          <th className="r" style={{ width: 130 }}>Credit</th><th>Line narration</th><th style={{ width: 110 }}>Bill ref</th><th style={{ width: 34 }}></th></tr></thead>
        <tbody>{lines.map((l, i) => (
          <tr key={i}>
            <td className="muted">{i + 1}</td>
            <td><select value={l.ledger_id} disabled={readOnly} onChange={(e) => setLine(i, 'ledger_id', e.target.value)}>
              <option value="">Choose a ledger…</option>
              {ledgers.map((L) => <option key={L.id} value={L.id}>{L.name} · {L.group}</option>)}</select></td>
            <td><input className="r" type="number" value={l.debit} disabled={readOnly} onChange={(e) => setLine(i, 'debit', e.target.value)} /></td>
            <td><input className="r" type="number" value={l.credit} disabled={readOnly} onChange={(e) => setLine(i, 'credit', e.target.value)} /></td>
            <td><input value={l.narration || ''} disabled={readOnly} onChange={(e) => setLine(i, 'narration', e.target.value)} /></td>
            <td><input value={l.bill_ref || ''} disabled={readOnly} onChange={(e) => setLine(i, 'bill_ref', e.target.value)} /></td>
            <td>{!readOnly && lines.length > 2 && <button className="rowdel" title="Remove line"
              onClick={() => setLines(lines.filter((_, j) => j !== i))}>×</button>}</td>
          </tr>
        ))}</tbody>
      </table></div>
      {!readOnly && <button className="btn sm" style={{ marginTop: 8 }} onClick={() => setLines([...lines, blankLine()])}>
        <Icon name="plus" size={13} /> Add line</button>}
      <F label="Narration" style={{ marginTop: 12 }}><textarea rows={2} value={h.narration || ''} disabled={readOnly}
        onChange={(e) => setH({ ...h, narration: e.target.value })} /></F>
    </Modal>
  )
}

const VCOLS = [['voucher_no', 'Voucher'], ['vtype', 'Type'], ['date', 'Date'], ['location', 'Location'],
  ['detail', 'Detail'], ['amount', 'Amount'], ['narration', 'Narration'], ['created_by', 'Created by']]

function VoucherTable({ rows, onOpen, onCancel, showDayTotals }) {
  let lastDate = null
  const out = []
  const dayTot = {}
  if (showDayTotals) rows.forEach((v) => { dayTot[v.date] = r2((dayTot[v.date] || 0) + v.amount) })
  rows.forEach((v, i) => {
    if (showDayTotals && v.date !== lastDate) {
      out.push(<tr key={'d' + v.date} className="fin-dayrow"><td colSpan={5}>{fmtDate(v.date)}</td>
        <td className="r">₹ {money(dayTot[v.date])}</td><td colSpan={3}></td></tr>)
      lastDate = v.date
    }
    const dr = v.lines?.filter((l) => l.debit) || []
    const cr = v.lines?.filter((l) => l.credit) || []
    out.push(
      <tr key={v.id} className={'clickable' + (v.status === 'cancelled' ? ' fin-void' : '')} onClick={() => onOpen(v)}>
        <td className="muted">{i + 1}</td>
        <td>{v.location || <span className="muted">—</span>}</td>
        <td><b>{v.vtype}</b>{!v.editable && <Badge tone="posted" title="Posted automatically from the warehouse or the till">auto</Badge>}</td>
        <td>{fmtDate(v.date)}</td>
        <td className="mono">{v.voucher_no}</td>
        <td className="wrap">
          {dr.slice(0, 2).map((l) => <div key={'d' + l.id}><span className="fin-dr">Dr</span> {l.ledger}</div>)}
          {cr.slice(0, 2).map((l) => <div key={'c' + l.id}><span className="fin-cr">Cr</span> {l.ledger}</div>)}
          {(dr.length > 2 || cr.length > 2) && <div className="small muted">+{dr.length + cr.length - 4} more</div>}
          {v.narration && <div className="small muted">{v.narration}</div>}
        </td>
        <td className="r">{money(v.amount)}</td>
        <td className="r">{money(v.amount)}</td>
        <td className="small muted">{v.created_by || ''}<div>{fmtDate(v.created_at?.slice(0, 10))}</div></td>
        <td className="acts" onClick={(e) => e.stopPropagation()}>
          {v.status === 'cancelled' ? <Badge tone="cancelled">cancelled</Badge>
            : v.editable && onCancel && <button className="btn sm danger" title="Cancel this voucher" onClick={() => onCancel(v)}>
              <Icon name="trash" size={13} /></button>}
        </td>
      </tr>)
  })
  return (
    <div className="tablewrap fin-scroll"><table className="items fin-table">
      <thead><tr><th style={{ width: 40 }}>S.No</th><th>Location</th><th>Voucher</th><th>Date</th><th>No</th>
        <th>Detail</th><th className="r">Debit</th><th className="r">Credit</th><th>Created</th><th></th></tr></thead>
      <tbody>{out}</tbody>
      <tfoot><tr><td colSpan={6} className="r">Total of {rows.length}</td>
        <td className="r b">{money(rows.reduce((a, v) => a + (v.status === 'cancelled' ? 0 : v.amount), 0))}</td>
        <td className="r b">{money(rows.reduce((a, v) => a + (v.status === 'cancelled' ? 0 : v.amount), 0))}</td><td colSpan={2}></td></tr></tfoot>
    </table></div>
  )
}

function VoucherEntry({ toast }) {
  const [o] = useFinOptions()
  const [ledgers, reloadLedgers] = useLedgers()
  const [q, setQ] = useState({ voucher_no: '', vtype: '', mode: '', ledger_id: '', amount_min: '', amount_max: '',
    date_from: todayIso(), date_to: todayIso() })
  const [rows, setRows] = useState(null)
  const [open, setOpen] = useState(null)
  const [newLedger, setNewLedger] = useState(false)
  const set = (k) => (v) => setQ((p) => ({ ...p, [k]: v?.target ? v.target.value : v }))
  const load = useCallback(() => {
    setRows(null)
    api.finGet('vouchers', q).then(setRows).catch((e) => { toast(errText(e), 'err'); setRows([]) })
  }, [q, toast])
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const cancel = async (v) => {
    if (!window.confirm(`Cancel ${v.vtype} ${v.voucher_no}? It stays on record, marked cancelled, and leaves every total.`)) return
    try { await api.finSend('DELETE', `vouchers/${v.id}`); toast(`${v.voucher_no} cancelled`, 'ok'); load() }
    catch (e) { toast(errText(e), 'err') }
  }
  return (
    <FinPage title="Voucher Entry" sub={rows ? `${rows.length} voucher${rows.length === 1 ? '' : 's'}` : ''}
      actions={<>
        <button className="btn" onClick={() => setNewLedger(true)}><Icon name="plus" size={14} /> New Ledger</button>
        <button className="btn primary" onClick={() => setOpen({})}><Icon name="plus" size={14} /> New Voucher</button>
        <button className="btn success" disabled={!rows?.length} onClick={() => downloadCsv('vouchers', VCOLS, rows)}>
          <Icon name="spreadsheet" size={14} /> Export</button></>}>
      <div className="fin-toolbar">
        <F label="Voucher No"><input value={q.voucher_no} onChange={set('voucher_no')} onKeyDown={(e) => e.key === 'Enter' && load()} /></F>
        <F label="Type"><Sel value={q.vtype} onChange={set('vtype')} placeholder="All" options={o.vtypes} /></F>
        <F label="Mode"><Sel value={q.mode} onChange={set('mode')} placeholder="All" options={o.modes} /></F>
        <F label="Ledger" style={{ minWidth: 200 }}><Sel value={q.ledger_id} onChange={set('ledger_id')} placeholder="Any ledger"
          options={ledgers.map((L) => ({ value: L.id, label: L.name }))} /></F>
        <F label="Range ₹"><div className="fin-inline">
          <input type="number" placeholder="min" value={q.amount_min} onChange={set('amount_min')} />
          <input type="number" placeholder="max" value={q.amount_max} onChange={set('amount_max')} /></div></F>
        <DateField label="From" value={q.date_from} onChange={set('date_from')} />
        <DateField label="To" value={q.date_to} onChange={set('date_to')} />
        <div className="field fin-go"><label>&nbsp;</label>
          <button className="btn primary" onClick={load}><Icon name="search" size={14} /> Search</button></div>
      </div>
      {rows === null ? <Loading>Reading the vouchers…</Loading> : rows.length === 0
        ? <Empty>No vouchers for these filters. Widen the dates, or press <b>New Voucher</b>.</Empty>
        : <VoucherTable rows={rows} onOpen={setOpen} onCancel={cancel} />}
      {open && <VoucherForm init={open.id ? open : null} toast={toast} onClose={() => setOpen(null)}
        onSaved={() => { setOpen(null); load() }} />}
      {newLedger && <LedgerForm toast={toast} onClose={() => setNewLedger(false)}
        onSaved={() => { setNewLedger(false); reloadLedgers() }} />}
    </FinPage>
  )
}

// ==========================================================================
//  Daybook
// ==========================================================================
function Daybook({ toast }) {
  const [o] = useFinOptions()
  const [from, setFrom] = useState(todayIso())
  const [to, setTo] = useState(todayIso())
  const [types, setTypes] = useState([])
  const [locs, setLocs] = useState([])
  const [rows, setRows] = useState(null)
  const [open, setOpen] = useState(null)
  const load = () => {
    setRows(null)
    api.finGet('daybook', { date_from: from, date_to: to, vtype: types, location: locs })
      .then((r) => setRows(r.slice().reverse())).catch((e) => { toast(errText(e), 'err'); setRows([]) })
  }
  useEffect(load, []) // eslint-disable-line react-hooks/exhaustive-deps
  const byType = useMemo(() => {
    const m = {}
    ;(rows || []).forEach((v) => { if (v.status !== 'cancelled') m[v.vtype] = r2((m[v.vtype] || 0) + v.amount) })
    return Object.entries(m)
  }, [rows])
  return (
    <FinPage title="Daybook" sub="Every voucher, day by day"
      actions={<button className="btn" disabled={!rows?.length} onClick={() => downloadCsv('daybook', VCOLS, rows)}>
        <Icon name="spreadsheet" size={14} /> Export</button>}
      side={<>
        <DateRange from={from} to={to} setFrom={setFrom} setTo={setTo} />
        <CheckList title="Voucher Types" items={o.vtypes} value={types} onChange={setTypes} />
        <CheckList title="Location" items={o.locations} value={locs} onChange={setLocs} />
        <button className="btn primary block" onClick={load}><Icon name="search" size={14} /> Search</button>
      </>}>
      {byType.length > 0 && <div className="fin-stats">
        {byType.map(([k, v]) => <Stat key={k} label={k} value={'₹ ' + money(v)} />)}</div>}
      {rows === null ? <Loading>Reading the daybook…</Loading> : rows.length === 0 ? <Empty>Nothing was entered on these days.</Empty>
        : <VoucherTable rows={rows} onOpen={setOpen} showDayTotals />}
      {open && <VoucherForm init={open} toast={toast} onClose={() => setOpen(null)} onSaved={() => { setOpen(null); load() }} />}
    </FinPage>
  )
}

// ==========================================================================
//  The statements: Trial Balance, Trading, P&L, Balance Sheet
// ==========================================================================
// Company + location + dates, the filter column the four statements share.
function StatementSide({ o, f, setF, asOn, stock, onRun, busy }) {
  const set = (k) => (v) => setF((p) => ({ ...p, [k]: v?.target ? v.target.value : v }))
  return (
    <>
      <F label="Company"><Sel value={f.business_id} onChange={set('business_id')} placeholder="All companies"
        options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
      <CheckList title="Location" items={o.locations} value={f.location} onChange={set('location')} />
      {asOn ? <DateField label="As on" value={f.as_on} onChange={set('as_on')} />
        : <DateRange from={f.date_from} to={f.date_to} setFrom={set('date_from')} setTo={set('date_to')} />}
      {stock && <>
        {!asOn && <F label="Opening stock ₹" hint="Stock at cost on the first day"><input type="number" value={f.opening_stock}
          onChange={set('opening_stock')} placeholder="0" /></F>}
        <F label="Closing stock ₹" hint="Blank: the warehouse's stock at cost today"><input type="number" value={f.closing_stock}
          onChange={set('closing_stock')} placeholder="from inventory" /></F>
      </>}
      <button className="btn primary block" disabled={busy} onClick={onRun}><Icon name="search" size={14} /> {busy ? 'Reading…' : 'Show'}</button>
    </>
  )
}

function useStatement(path, initial, toast) {
  const [f, setF] = useState(initial)
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const run = useCallback(() => {
    setBusy(true)
    api.finGet(path, f).then(setData).catch((e) => toast(errText(e), 'err')).finally(() => setBusy(false))
  }, [path, f, toast])
  useEffect(() => { run() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return { f, setF, data, busy, run }
}

function TrialBalance({ toast }) {
  const [o] = useFinOptions()
  const s = useStatement('trial-balance', { business_id: '', location: [], date_from: fyStart(), date_to: todayIso() }, toast)
  const [open, setOpen] = useState({})
  const [stmt, setStmt] = useState(null)
  const d = s.data
  return (
    <FinPage title="Trial Balance" sub={d ? (d.totals.difference ? `Out by ₹${money(Math.abs(d.totals.difference))}` : 'Balanced') : ''}
      actions={d && <>
        <button className="btn" onClick={() => setOpen(Object.fromEntries(d.groups.map((g) => [g.group, true])))}>Expand all</button>
        <button className="btn" onClick={() => downloadCsv('trial-balance', [['group', 'Group'], ['ledger', 'Ledger'],
          ['opening', 'Opening'], ['debit', 'Debit'], ['credit', 'Credit'], ['closing_dr', 'Closing Dr'], ['closing_cr', 'Closing Cr']],
        d.groups.flatMap((g) => g.rows.map((r) => ({ ...r, group: g.group }))))}><Icon name="spreadsheet" size={14} /> Export</button></>}
      side={<StatementSide o={o} {...s} onRun={s.run} />}>
      {!d ? <Loading>Reading the books…</Loading> : (
        <>
          <div className="fin-stats">
            <Stat label="Debit (period)" value={'₹ ' + money(d.totals.debit)} />
            <Stat label="Credit (period)" value={'₹ ' + money(d.totals.credit)} />
            <Stat label="Closing Dr" value={'₹ ' + money(d.totals.closing_dr)} />
            <Stat label="Closing Cr" value={'₹ ' + money(d.totals.closing_cr)} />
            <Stat label="Difference" value={'₹ ' + money(d.totals.difference)} accent={d.totals.difference ? 'warn' : 'money'} />
          </div>
          <div className="tablewrap fin-scroll"><table className="items fin-table">
            <thead><tr><th>Particulars</th><th className="r">Opening</th><th className="r">Debit</th><th className="r">Credit</th>
              <th className="r">Closing Dr</th><th className="r">Closing Cr</th></tr></thead>
            <tbody>{d.groups.map((g) => (
              <React.Fragment key={g.group}>
                <tr className="fin-grouprow clickable" onClick={() => setOpen({ ...open, [g.group]: !open[g.group] })}>
                  <td><span className="fin-caret">{open[g.group] ? '▾' : '▸'}</span> {g.group} <span className="small muted">· {g.rows.length}</span></td>
                  <td className="r">{drcr(g.opening)}</td><td className="r">{amt(g.debit)}</td><td className="r">{amt(g.credit)}</td>
                  <td className="r b">{amt(g.closing_dr)}</td><td className="r b">{amt(g.closing_cr)}</td></tr>
                {open[g.group] && g.rows.map((r) => (
                  <tr key={r.ledger_id} className="fin-subrow clickable" onClick={() => setStmt({ id: r.ledger_id, name: r.ledger })}>
                    <td>{r.ledger}</td><td className="r">{drcr(r.opening)}</td><td className="r">{amt(r.debit)}</td>
                    <td className="r">{amt(r.credit)}</td><td className="r">{amt(r.closing_dr)}</td><td className="r">{amt(r.closing_cr)}</td></tr>
                ))}
              </React.Fragment>
            ))}</tbody>
            <tfoot><tr><td>Total</td><td></td><td className="r">{money(d.totals.debit)}</td><td className="r">{money(d.totals.credit)}</td>
              <td className="r">{money(d.totals.closing_dr)}</td><td className="r">{money(d.totals.closing_cr)}</td></tr></tfoot>
          </table></div>
        </>
      )}
      {stmt && <StatementModal ledger={stmt} onClose={() => setStmt(null)} />}
    </FinPage>
  )
}

// One side of a T-account: groups, each with its ledgers beneath.
function TSide({ title, groups, extra, total, onLedger }) {
  return (
    <div className="tside">
      <div className="tside-head">{title}</div>
      <div className="tside-body">
        {extra.filter((x) => x && x.amount).map((x) => (
          <div key={x.label} className={'trow strong' + (x.tone ? ' ' + x.tone : '')}><span>{x.label}</span><span>{money(x.amount)}</span></div>
        ))}
        {groups.map((g) => (
          <div key={g.group} className="tgroup">
            <div className="trow strong"><span>{g.group}</span><span>{money(g.amount)}</span></div>
            {g.rows.map((r, i) => (
              <div key={i} className={'trow sub' + (r.ledger_id ? ' clickable' : '')}
                onClick={() => r.ledger_id && onLedger({ id: r.ledger_id, name: r.ledger })}>
                <span>{r.ledger}</span><span>{money(r.amount)}</span></div>
            ))}
          </div>
        ))}
        {groups.length === 0 && !extra.some((x) => x && x.amount) && <div className="trow sub muted"><span>Nothing in this period</span><span /></div>}
      </div>
      <div className="tside-foot"><span>Total</span><span>₹ {money(total)}</span></div>
    </div>
  )
}

function TradingAccount({ toast }) {
  const [o] = useFinOptions()
  const s = useStatement('trading', { business_id: '', location: [], date_from: fyStart(), date_to: todayIso(),
    opening_stock: '', closing_stock: '' }, toast)
  const [stmt, setStmt] = useState(null)
  const d = s.data
  return (
    <FinPage title="Trading Account" sub={d ? `Gross ${d.gross_profit >= 0 ? 'profit' : 'loss'} ₹${money(Math.abs(d.gross_profit))}` : ''}
      side={<StatementSide o={o} {...s} stock onRun={s.run} />}>
      {!d ? <Loading>Reading the books…</Loading> : (
        <div className="tform">
          <TSide title="Dr · Purchases & direct expenses" groups={d.expense} onLedger={setStmt} total={d.total}
            extra={[{ label: 'Opening stock', amount: d.opening_stock },
              d.gross_profit > 0 && { label: 'Gross profit c/o', amount: d.gross_profit, tone: 'ok' }]} />
          <TSide title="Cr · Sales & direct income" groups={d.income} onLedger={setStmt} total={d.total}
            extra={[{ label: 'Closing stock', amount: d.closing_stock },
              d.gross_profit < 0 && { label: 'Gross loss c/o', amount: -d.gross_profit, tone: 'bad' }]} />
        </div>
      )}
      {stmt && <StatementModal ledger={stmt} onClose={() => setStmt(null)} />}
    </FinPage>
  )
}

function ProfitLoss({ toast }) {
  const [o] = useFinOptions()
  const s = useStatement('pl', { business_id: '', location: [], date_from: fyStart(), date_to: todayIso(),
    opening_stock: '', closing_stock: '' }, toast)
  const [stmt, setStmt] = useState(null)
  const d = s.data
  return (
    <FinPage title="Profit & Loss Account" sub={d ? `Net ${d.net_profit >= 0 ? 'profit' : 'loss'} ₹${money(Math.abs(d.net_profit))}` : ''}
      side={<StatementSide o={o} {...s} stock onRun={s.run} />}>
      {!d ? <Loading>Reading the books…</Loading> : (
        <>
          <div className="fin-stats">
            <Stat label="Gross profit" value={'₹ ' + money(d.gross_profit)} accent={d.gross_profit < 0 ? 'warn' : 'money'} />
            <Stat label="Net profit" value={'₹ ' + money(d.net_profit)} accent={d.net_profit < 0 ? 'warn' : 'money'} />
          </div>
          <div className="tform">
            <TSide title="Dr · Indirect expenses" groups={d.expense} onLedger={setStmt} total={d.total}
              extra={[d.gross_profit < 0 && { label: 'Gross loss b/f', amount: -d.gross_profit, tone: 'bad' },
                d.net_profit > 0 && { label: 'Net profit', amount: d.net_profit, tone: 'ok' }]} />
            <TSide title="Cr · Indirect income" groups={d.income} onLedger={setStmt} total={d.total}
              extra={[d.gross_profit > 0 && { label: 'Gross profit b/f', amount: d.gross_profit, tone: 'ok' },
                d.net_profit < 0 && { label: 'Net loss', amount: -d.net_profit, tone: 'bad' }]} />
          </div>
        </>
      )}
      {stmt && <StatementModal ledger={stmt} onClose={() => setStmt(null)} />}
    </FinPage>
  )
}

function BalanceSheet({ toast }) {
  const [o] = useFinOptions()
  const s = useStatement('balance-sheet', { business_id: '', location: [], as_on: todayIso(), closing_stock: '' }, toast)
  const [stmt, setStmt] = useState(null)
  const d = s.data
  return (
    <FinPage title="Balance Sheet" sub={d ? `As on ${fmtDate(d.as_on)}${d.difference ? ` · out by ₹${money(Math.abs(d.difference))}` : ' · tallies'}` : ''}
      side={<StatementSide o={o} {...s} asOn stock onRun={s.run} />}>
      {!d ? <Loading>Reading the books…</Loading> : (
        <>
          {d.difference !== 0 && <div className="fin-warn">The two sides differ by ₹{money(Math.abs(d.difference))}. That is
            almost always an opening balance that does not balance — see <b>Opening Balance</b>.</div>}
          <div className="tform">
            <TSide title="Liabilities" groups={d.liabilities} onLedger={setStmt} total={d.total_liabilities} extra={[]} />
            <TSide title="Assets" groups={d.assets} onLedger={setStmt} total={d.total_assets} extra={[]} />
          </div>
        </>
      )}
      {stmt && <StatementModal ledger={stmt} onClose={() => setStmt(null)} />}
    </FinPage>
  )
}

function DailyPL({ toast }) {
  const [o] = useFinOptions()
  const s = useStatement('daily-pl', { business_id: '', location: '', date_from: monthStart(), date_to: todayIso() }, toast)
  const d = s.data
  const set = (k) => (v) => s.setF((p) => ({ ...p, [k]: v }))
  const peak = d ? Math.max(1, ...d.rows.map((r) => Math.max(r.income, r.expense))) : 1
  return (
    <FinPage title="Daily P&L" sub="What each day earned and spent"
      actions={d && <button className="btn" onClick={() => downloadCsv('daily-pl', [['date', 'Date'], ['sales', 'Sales'],
        ['purchase', 'Purchase'], ['income', 'Income'], ['expense', 'Expense'], ['profit', 'Profit']], d.rows)}>
        <Icon name="spreadsheet" size={14} /> Export</button>}
      side={<>
        <F label="Company"><Sel value={s.f.business_id} onChange={set('business_id')} placeholder="All companies"
          options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <F label="Location"><Sel value={s.f.location} onChange={set('location')} placeholder="All locations" options={o.locations} /></F>
        <DateRange from={s.f.date_from} to={s.f.date_to} setFrom={set('date_from')} setTo={set('date_to')} />
        <button className="btn primary block" onClick={s.run}><Icon name="search" size={14} /> Show</button>
      </>}>
      {!d ? <Loading>Reading the books…</Loading> : (
        <>
          <div className="fin-stats">
            <Stat label="Sales" value={'₹ ' + money(d.totals.sales)} />
            <Stat label="Purchase" value={'₹ ' + money(d.totals.purchase)} />
            <Stat label="All income" value={'₹ ' + money(d.totals.income)} />
            <Stat label="All expense" value={'₹ ' + money(d.totals.expense)} />
            <Stat label="Profit" value={'₹ ' + money(d.totals.profit)} accent={d.totals.profit < 0 ? 'warn' : 'money'} />
          </div>
          {d.rows.length === 0 ? <Empty>No income or expense in this period.</Empty> : (
            <div className="tablewrap fin-scroll"><table className="items fin-table">
              <thead><tr><th>Date</th><th className="r">Sales</th><th className="r">Purchase</th><th className="r">Income</th>
                <th className="r">Expense</th><th style={{ width: '22%' }}><span className="fin-key inc">income</span> <span className="fin-key exp">expense</span></th>
                <th className="r">Profit</th></tr></thead>
              <tbody>{d.rows.map((r) => (
                <tr key={r.date}><td>{fmtDate(r.date)}</td><td className="r">{amt(r.sales)}</td><td className="r">{amt(r.purchase)}</td>
                  <td className="r">{amt(r.income)}</td><td className="r">{amt(r.expense)}</td>
                  <td><div className="fin-bars"><i className="inc" style={{ width: `${(100 * r.income) / peak}%` }} />
                    <i className="exp" style={{ width: `${(100 * r.expense) / peak}%` }} /></div></td>
                  <td className={'r b ' + (r.profit < 0 ? 'neg' : 'pos')}>{money(r.profit)}</td></tr>
              ))}</tbody>
            </table></div>
          )}
        </>
      )}
    </FinPage>
  )
}

// ==========================================================================
//  Payable / Receivable — aged outstanding
// ==========================================================================
function BillsModal({ party, onClose }) {
  const [d, setD] = useState(null)
  useEffect(() => { api.finGet(`ledgers/${party.ledger_id}/bills`).then(setD).catch(() => setD({ bills: [], pending: 0 })) }, [party])
  return (
    <Modal title={`Pending bills — ${party.party}`} onClose={onClose} wide
      foot={<><div className="spacer" /><button className="btn primary" onClick={onClose}>Close</button></>}>
      {!d ? <Loading>Reading…</Loading> : (
        <>
          <div className="fin-stats"><Stat label="Pending" value={'₹ ' + money(d.pending)} accent="money" />
            {d.on_account ? <Stat label="On account" value={'₹ ' + money(d.on_account)} /> : null}</div>
          {d.bills.length === 0 ? <Empty>No bill-wise items — the balance is on account.</Empty> : (
            <table className="items fin-table"><thead><tr><th>Date</th><th>Voucher</th><th>Bill</th><th>Narration</th>
              <th className="r">Days</th><th className="r">Amount</th><th className="r">Settled</th><th className="r">Pending</th></tr></thead>
            <tbody>{d.bills.map((b) => (
              <tr key={b.bill_ref}><td>{fmtDate(b.date)}</td><td className="mono">{b.voucher_no}</td><td className="mono">{b.bill_ref}</td>
                <td className="wrap">{b.narration}</td><td className="r">{b.days ?? ''}</td><td className="r">{money(b.amount)}</td>
                <td className="r">{amt(b.settled)}</td><td className="r b">{money(b.pending)}</td></tr>
            ))}</tbody></table>
          )}
        </>
      )}
    </Modal>
  )
}

function Outstanding({ toast, side }) {
  const [o] = useFinOptions()
  const [f, setF] = useState({ as_on: todayIso(), location: '', q: '' })
  const [d, setD] = useState(null)
  const [pick, setPick] = useState(null)
  const load = () => { setD(null); api.finGet(`outstanding/${side}`, f).then(setD).catch((e) => toast(errText(e), 'err')) }
  useEffect(load, [side]) // eslint-disable-line react-hooks/exhaustive-deps
  const title = side === 'payable' ? 'Payable' : 'Receivable'
  return (
    <FinPage title={title} sub={side === 'payable' ? 'What we owe suppliers, transporters and agents' : 'What customers owe us'}
      actions={d && <button className="btn" onClick={() => downloadCsv(side, [['party', 'Party'], ['gstin', 'GSTIN'],
        ...d.buckets.map((b) => [b, b + ' days']), ['pending', 'Pending']], d.rows)}><Icon name="spreadsheet" size={14} /> Download</button>}
      side={<>
        <DateField label="As on" value={f.as_on} onChange={(v) => setF({ ...f, as_on: v })} />
        <F label="Location"><Sel value={f.location} onChange={(v) => setF({ ...f, location: v })} placeholder="All locations" options={o.locations} /></F>
        <F label="Party"><input value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} placeholder="Name contains…"
          onKeyDown={(e) => e.key === 'Enter' && load()} /></F>
        <button className="btn primary block" onClick={load}><Icon name="search" size={14} /> Search</button>
      </>}>
      {!d ? <Loading>Reading the books…</Loading> : (
        <>
          <div className="fin-stats">
            <Stat label={`Total ${title.toLowerCase()}`} value={'₹ ' + money(d.totals.pending)} accent="money" />
            <Stat label="Parties" value={d.rows.length} accent="count" />
            {d.buckets.map((b) => <Stat key={b} label={b + ' days'} value={'₹ ' + money(d.totals[b])} />)}
          </div>
          {d.rows.length === 0 ? <Empty>Nothing outstanding as on {fmtDate(d.as_on)}.</Empty> : (
            <div className="tablewrap fin-scroll"><table className="items fin-table">
              <thead><tr><th>Party</th><th>GSTIN</th>{d.buckets.map((b) => <th key={b} className="r">{b}</th>)}
                <th className="r">Pending</th></tr></thead>
              <tbody>{d.rows.map((r) => (
                <tr key={r.ledger_id} className="clickable" onClick={() => setPick(r)}>
                  <td><b>{r.party}</b>{r.phone && <div className="small muted">{r.phone}</div>}</td>
                  <td className="mono">{r.gstin || ''}</td>
                  {d.buckets.map((b, i) => <td key={b} className={'r' + (i >= 3 && r[b] ? ' neg' : '')}>{amt(r[b])}</td>)}
                  <td className="r b">{money(r.pending)}</td></tr>
              ))}</tbody>
              <tfoot><tr><td colSpan={2}>Total</td>{d.buckets.map((b) => <td key={b} className="r">{money(d.totals[b])}</td>)}
                <td className="r">{money(d.totals.pending)}</td></tr></tfoot>
            </table></div>
          )}
        </>
      )}
      {pick && <BillsModal party={pick} onClose={() => setPick(null)} />}
    </FinPage>
  )
}

// ==========================================================================
//  TDS Transactions and the Analyser
// ==========================================================================
function TdsTransactions({ toast }) {
  const [o] = useFinOptions()
  const [ledgers] = useLedgers({ filter: 'tds' })
  const [f, setF] = useState({ business_id: '', location: [], vtype: [], ledger: [], date_from: fyStart(), date_to: todayIso() })
  const [d, setD] = useState(null)
  const set = (k) => (v) => setF((p) => ({ ...p, [k]: v }))
  const load = () => { setD(null); api.finGet('tds', f).then(setD).catch((e) => toast(errText(e), 'err')) }
  useEffect(load, []) // eslint-disable-line react-hooks/exhaustive-deps
  const cols = [['date', 'Date'], ['voucher_no', 'Voucher'], ['vtype', 'Type'], ['party', 'Party'], ['pan', 'PAN'],
    ['section', 'Section'], ['ledger', 'TDS ledger'], ['gross', 'Gross'], ['tds', 'TDS']]
  return (
    <FinPage title="TDS Transactions"
      actions={<>
        <button className="btn" onClick={load}><Icon name="search" size={14} /> Search</button>
        <button className="btn" disabled={!d?.rows.length} onClick={() => downloadCsv('tds', cols, d.rows)}>
          <Icon name="spreadsheet" size={14} /> Export</button></>}
      side={<>
        <F label="Company"><Sel value={f.business_id} onChange={set('business_id')} placeholder="All companies"
          options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <CheckList title="Location" items={o.locations} value={f.location} onChange={set('location')} />
        <CheckList title="Voucher Types" items={o.vtypes} value={f.vtype} onChange={set('vtype')} />
        <CheckList title="Ledger" items={ledgers.map((L) => ({ value: String(L.id), label: L.name }))} value={f.ledger} onChange={set('ledger')} />
        <DateRange from={f.date_from} to={f.date_to} setFrom={set('date_from')} setTo={set('date_to')} />
      </>}>
      {!d ? <Loading>Reading…</Loading> : (
        <>
          <div className="fin-stats"><Stat label="TDS" value={'₹ ' + money(d.total)} accent="money" />
            <Stat label="Entries" value={d.rows.length} accent="count" /></div>
          {d.rows.length === 0 ? <Empty>No TDS was deducted in this period. Mark a ledger <b>TDS applicable</b> in Manage Ledger,
            or enter TDS on a supplier, transport or agent payment.</Empty> : (
            <div className="tablewrap fin-scroll"><table className="items fin-table">
              <thead><tr><th>Date</th><th>Voucher</th><th>Type</th><th>Party</th><th>PAN</th><th>Section</th>
                <th>TDS ledger</th><th className="r">Gross</th><th className="r">TDS</th></tr></thead>
              <tbody>{d.rows.map((r, i) => (
                <tr key={i}><td>{fmtDate(r.date)}</td><td className="mono">{r.voucher_no}</td><td>{r.vtype}</td><td>{r.party || '—'}</td>
                  <td className="mono">{r.pan || <Badge tone="pending">no PAN</Badge>}</td><td>{r.section || ''}</td><td>{r.ledger}</td>
                  <td className="r">{money(r.gross)}</td><td className="r b">{money(r.tds)}</td></tr>
              ))}</tbody>
            </table></div>
          )}
        </>
      )}
    </FinPage>
  )
}

const ANALYSER_MODES = [{ value: 'group', label: 'Group summary' }, { value: 'ledger', label: 'Ledger summary' },
  { value: 'month', label: 'Month-wise' }]

function Analyser({ toast }) {
  const [o] = useFinOptions()
  const [f, setF] = useState({ business_id: '', location: [], vtype: [], group: [], mode: 'group',
    date_from: fyStart(), date_to: todayIso() })
  const [d, setD] = useState(null)
  const set = (k) => (v) => setF((p) => ({ ...p, [k]: v }))
  const load = () => { setD(null); api.finGet('analyser', f).then(setD).catch((e) => toast(errText(e), 'err')) }
  useEffect(load, []) // eslint-disable-line react-hooks/exhaustive-deps
  const peak = d ? Math.max(1, ...d.rows.map((r) => Math.abs(r.net))) : 1
  const head = ANALYSER_MODES.find((m) => m.value === (d?.mode || f.mode))?.label
  return (
    <FinPage title="Analyser" sub={head}
      actions={<>
        <button className="btn" onClick={load}><Icon name="search" size={14} /> Search</button>
        <button className="btn" disabled={!d?.rows.length} onClick={() => downloadCsv('analyser', [['key', head],
          ['debit', 'Debit'], ['credit', 'Credit'], ['net', 'Net']], d.rows)}><Icon name="spreadsheet" size={14} /> Export</button></>}
      side={<>
        <F label="Company"><Sel value={f.business_id} onChange={set('business_id')} placeholder="All companies"
          options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <CheckList title="Location" items={o.locations} value={f.location} onChange={set('location')} />
        <CheckList title="Voucher Types" items={o.vtypes} value={f.vtype} onChange={set('vtype')} />
        <CheckList title="Ledger Type" items={o.groups.map((g) => g.name)} value={f.group} onChange={set('group')} />
        <F label="Report Mode"><Sel value={f.mode} allowBlank={false} onChange={set('mode')} options={ANALYSER_MODES} /></F>
        <DateRange from={f.date_from} to={f.date_to} setFrom={set('date_from')} setTo={set('date_to')} />
      </>}>
      {!d ? <Loading>Reading…</Loading> : d.rows.length === 0 ? <Empty>Nothing moved for these filters.</Empty> : (
        <div className="tablewrap fin-scroll"><table className="items fin-table">
          <thead><tr><th>{head}</th><th className="r">Debit</th><th className="r">Credit</th><th style={{ width: '24%' }}></th><th className="r">Net</th></tr></thead>
          <tbody>{d.rows.map((r) => (
            <tr key={r.key}><td>{d.mode === 'month' ? r.key : <b>{r.key}</b>}</td><td className="r">{amt(r.debit)}</td>
              <td className="r">{amt(r.credit)}</td>
              <td><div className="fin-bars"><i className={r.net >= 0 ? 'inc' : 'exp'} style={{ width: `${(100 * Math.abs(r.net)) / peak}%` }} /></div></td>
              <td className="r b">{drcr(r.net)}</td></tr>
          ))}</tbody>
          <tfoot><tr><td>Total</td><td className="r">{money(d.totals.debit)}</td><td className="r">{money(d.totals.credit)}</td><td></td>
            <td className="r">{drcr(d.totals.debit - d.totals.credit)}</td></tr></tfoot>
        </table></div>
      )}
    </FinPage>
  )
}

// ==========================================================================
//  Loan Reconciliation, GST Inbox, Opening Balance
// ==========================================================================
function LoanReconciliation({ toast }) {
  const [f, setF] = useState({ date_from: fyStart(), as_on: todayIso() })
  const [rows, setRows] = useState(null)
  const [stmt, setStmt] = useState(null)
  const [add, setAdd] = useState(false)
  const load = () => { setRows(null); api.finGet('loans', f).then(setRows).catch((e) => toast(errText(e), 'err')) }
  useEffect(load, []) // eslint-disable-line react-hooks/exhaustive-deps
  const tot = (k) => r2((rows || []).reduce((a, r) => a + r[k], 0))
  return (
    <FinPage title="Loan Reconciliation" sub="Every loan taken and given: received, repaid and what is left"
      actions={<button className="btn primary" onClick={() => setAdd(true)}><Icon name="plus" size={14} /> New loan ledger</button>}
      side={<>
        <DateRange from={f.date_from} to={f.as_on} setFrom={(v) => setF({ ...f, date_from: v })} setTo={(v) => setF({ ...f, as_on: v })} />
        <button className="btn primary block" onClick={load}><Icon name="search" size={14} /> Show</button>
      </>}>
      {!rows ? <Loading>Reading…</Loading> : rows.length === 0 ? (
        <Empty>No loan ledgers yet. A loan is a ledger of type <b>Loans (Liability)</b> (money borrowed) or
          <b> Loans &amp; Advances (Asset)</b> (money lent); its instalments are Payment vouchers against it.</Empty>
      ) : (
        <>
          <div className="fin-stats"><Stat label="Outstanding" value={'₹ ' + money(tot('closing'))} accent="money" />
            <Stat label="Received" value={'₹ ' + money(tot('received'))} /><Stat label="Repaid" value={'₹ ' + money(tot('repaid'))} /></div>
          <div className="tablewrap fin-scroll"><table className="items fin-table">
            <thead><tr><th>Loan</th><th>Type</th><th>Account</th><th className="r">Opening</th><th className="r">Received</th>
              <th className="r">Repaid</th><th className="r">Outstanding</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.ledger_id} className="clickable" onClick={() => setStmt({ id: r.ledger_id, name: r.loan })}>
                <td><b>{r.loan}</b></td><td>{r.group}</td><td className="mono">{r.bank_account || ''}</td>
                <td className="r">{money(r.opening)}</td><td className="r">{amt(r.received)}</td><td className="r">{amt(r.repaid)}</td>
                <td className="r b">{money(r.closing)}</td></tr>
            ))}</tbody>
          </table></div>
        </>
      )}
      {stmt && <StatementModal ledger={stmt} onClose={() => setStmt(null)} />}
      {add && <LedgerForm init={{ group: 'Loans (Liability)' }} toast={toast} onClose={() => setAdd(false)}
        onSaved={() => { setAdd(false); load() }} />}
    </FinPage>
  )
}

function GstInbox({ toast }) {
  const [f, setF] = useState({ date_from: monthStart(), date_to: todayIso(), supplier: '' })
  const [d, setD] = useState(null)
  const [only, setOnly] = useState('all')
  const load = () => { setD(null); api.finGet('gst-inbox', f).then(setD).catch((e) => toast(errText(e), 'err')) }
  useEffect(load, []) // eslint-disable-line react-hooks/exhaustive-deps
  const rows = (d?.rows || []).filter((r) => only === 'all' || (only === 'flagged' ? r.issues.length : !r.issues.length))
  return (
    <FinPage title="GST Inbox" sub="Input tax on every purchase invoice, checked before it is claimed"
      actions={d && <button className="btn" onClick={() => downloadCsv('gst-inbox', [['date', 'Date'], ['invoice_no', 'Invoice'],
        ['supplier', 'Supplier'], ['gstin', 'GSTIN'], ['taxable', 'Taxable'], ['rate', 'Rate %'], ['tax', 'Tax'], ['total', 'Total'],
        [(r) => r.issues.join('; '), 'Issues']], rows)}><Icon name="spreadsheet" size={14} /> Export</button>}
      side={<>
        <DateRange from={f.date_from} to={f.date_to} setFrom={(v) => setF({ ...f, date_from: v })} setTo={(v) => setF({ ...f, date_to: v })} />
        <F label="Supplier"><input value={f.supplier} onChange={(e) => setF({ ...f, supplier: e.target.value })} placeholder="Name contains…" /></F>
        <button className="btn primary block" onClick={load}><Icon name="search" size={14} /> Search</button>
      </>}>
      {!d ? <Loading>Reading the purchase register…</Loading> : (
        <>
          <div className="fin-stats">
            <Stat label="Invoices" value={d.rows.length} accent="count" />
            <Stat label="Taxable" value={'₹ ' + money(d.totals.taxable)} />
            <Stat label="Input GST" value={'₹ ' + money(d.totals.tax)} accent="money" />
            <Stat label="Need attention" value={d.totals.flagged} accent={d.totals.flagged ? 'warn' : 'count'} />
          </div>
          <div className="fin-chips">
            {[['all', 'All'], ['flagged', 'Needs attention'], ['clean', 'Ready to claim']].map(([k, l]) => (
              <button key={k} className={'fin-chip' + (only === k ? ' on' : '')} onClick={() => setOnly(k)}>{l}</button>))}
          </div>
          {rows.length === 0 ? <Empty>No posted purchase invoices here.</Empty> : (
            <div className="tablewrap fin-scroll"><table className="items fin-table">
              <thead><tr><th>Date</th><th>Invoice</th><th>Supplier</th><th>GSTIN</th><th className="r">Taxable</th>
                <th className="r">Rate</th><th className="r">Tax</th><th className="r">Total</th><th>Status</th></tr></thead>
              <tbody>{rows.map((r) => (
                <tr key={r.purchase_id}><td>{fmtDate(r.date)}</td><td className="mono">{r.invoice_no}<div className="small muted">{r.grn_no}</div></td>
                  <td>{r.supplier}</td><td className="mono">{r.gstin || '—'}</td><td className="r">{money(r.taxable)}</td>
                  <td className="r">{r.rate ? r.rate + '%' : ''}</td><td className="r">{money(r.tax)}</td><td className="r b">{money(r.total)}</td>
                  <td>{r.issues.length ? r.issues.map((x) => <Badge key={x} tone="pending">{x}</Badge>) : <Badge tone="confirmed">ok</Badge>}</td></tr>
              ))}</tbody>
            </table></div>
          )}
        </>
      )}
    </FinPage>
  )
}

function OpeningBalance({ toast }) {
  const [o] = useFinOptions()
  const [d, setD] = useState(null)
  const [edits, setEdits] = useState({})
  const [group, setGroup] = useState('')
  const [q, setQ] = useState('')
  const [busy, run] = useRun(toast)
  const load = () => api.finGet('opening').then((x) => { setD(x); setEdits({}) }).catch((e) => toast(errText(e), 'err'))
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const val = (r) => (edits[r.id] !== undefined ? edits[r.id] : r.opening_balance)
  const all = d?.rows || []
  const dr = r2(all.reduce((a, r) => a + Math.max(0, +val(r) || 0), 0))
  const cr = r2(all.reduce((a, r) => a + Math.max(0, -(+val(r) || 0)), 0))
  const shown = all.filter((r) => (!group || r.group === group) && (!q || r.name.toLowerCase().includes(q.toLowerCase())))
  const setSide = (r, side, v) => setEdits({ ...edits, [r.id]: side === 'dr' ? (+v || 0) : -(+v || 0) })
  const save = async () => {
    const rows = Object.entries(edits).map(([id, v]) => ({ ledger_id: +id, opening_balance: +v || 0 }))
    const r = await run(() => api.finSend('PUT', 'opening', rows), (x) => `✓ ${x.saved} opening balance(s) saved`)
    if (r) load()
  }
  const dirty = Object.keys(edits).length
  return (
    <FinPage title="Opening Balance" sub="What every ledger held on the day these books start"
      actions={<button className="btn primary" disabled={busy || !dirty} onClick={save}>{busy ? 'Saving…' : `Save${dirty ? ` (${dirty})` : ''}`}</button>}>
      <div className="fin-toolbar">
        <F label="Type"><Sel value={group} onChange={setGroup} placeholder="All types" options={o.groups.map((g) => g.name)} /></F>
        <F label="Ledger" style={{ flex: 1, maxWidth: 320 }}><input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name contains…" /></F>
        <div className="fin-stats inline">
          <Stat label="Debit" value={'₹ ' + money(dr)} /><Stat label="Credit" value={'₹ ' + money(cr)} />
          <Stat label="Difference" value={'₹ ' + money(dr - cr)} accent={r2(dr - cr) ? 'warn' : 'money'} />
        </div>
      </div>
      {r2(dr - cr) !== 0 && <div className="fin-warn">Debits and credits must be equal before the balance sheet will tally —
        the difference usually belongs in <b>Capital</b>.</div>}
      {!d ? <Loading>Reading the ledgers…</Loading> : (
        <div className="tablewrap fin-scroll"><table className="items fin-table">
          <thead><tr><th>Ledger</th><th>Type</th><th className="r" style={{ width: 160 }}>Debit</th><th className="r" style={{ width: 160 }}>Credit</th></tr></thead>
          <tbody>{shown.map((r) => {
            const v = +val(r) || 0
            return (
              <tr key={r.id} className={edits[r.id] !== undefined ? 'fin-edited' : ''}><td><b>{r.name}</b></td><td>{r.group}</td>
                <td><input className="r" type="number" value={v > 0 ? v : ''} onChange={(e) => setSide(r, 'dr', e.target.value)} /></td>
                <td><input className="r" type="number" value={v < 0 ? -v : ''} onChange={(e) => setSide(r, 'cr', e.target.value)} /></td></tr>
            )
          })}</tbody>
        </table></div>
      )}
    </FinPage>
  )
}

// ==========================================================================
//  BRS Book — bank reconciliation
// ==========================================================================
const BRS_MODES = ['Supplier', 'Transport', 'Agent', 'Customer', 'Voucher']

function BankReconciliation({ toast }) {
  const [o] = useFinOptions()
  const [f, setF] = useState({ bank_id: '', status: '', modes: [], date_from: monthStart(), date_to: todayIso() })
  const [d, setD] = useState(null)
  const set = (k) => (v) => setF((p) => ({ ...p, [k]: v }))
  const load = () => {
    if (!f.bank_id) { toast('Choose the bank first', 'err'); return }
    setD(null)
    api.finGet('brs', f).then(setD).catch((e) => toast(errText(e), 'err'))
  }
  useEffect(() => { if (!f.bank_id && o.banks.length) set('bank_id')(String(o.banks[0].id)) }, [o.banks.length]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (f.bank_id) load() }, [f.bank_id]) // eslint-disable-line react-hooks/exhaustive-deps
  const clear = async (row, date) => {
    try {
      await api.finSend('PUT', `brs/line/${row.line_id}`, { cleared_on: date || null })
      setD((x) => ({ ...x, rows: x.rows.map((r) => (r.line_id === row.line_id ? { ...r, cleared_on: date || null } : r)) }))
    } catch (e) { toast(errText(e), 'err') }
  }
  return (
    <FinPage title="Bank Reconciliation" sub="Mark each entry with the day the bank statement shows it"
      actions={d && <button className="btn" onClick={() => downloadCsv('brs', [['date', 'Date'], ['voucher_no', 'Voucher'],
        ['narration', 'Particulars'], ['cheque_no', 'Cheque'], ['debit', 'Deposit'], ['credit', 'Withdrawal'], ['cleared_on', 'Bank date']],
      d.rows)}><Icon name="spreadsheet" size={14} /> Export</button>}
      side={<>
        <F label="Bank" req><Sel value={f.bank_id} onChange={set('bank_id')} placeholder="Choose a bank"
          options={o.banks.map((b) => ({ value: String(b.id), label: b.name }))} /></F>
        <F label="Status"><Sel value={f.status} onChange={set('status')} placeholder="All"
          options={[{ value: 'uncleared', label: 'Not yet in the bank' }, { value: 'cleared', label: 'Cleared' }]} /></F>
        <CheckList title="Transaction Modes" items={BRS_MODES} value={f.modes} onChange={set('modes')} height={150} />
        <DateRange from={f.date_from} to={f.date_to} setFrom={set('date_from')} setTo={set('date_to')} />
        <button className="btn primary block" onClick={load}><Icon name="search" size={14} /> Search</button>
      </>}>
      {o.banks.length === 0 ? <Empty>No bank ledgers yet. Create one in <b>Manage Ledger</b> with the type <b>Bank Account</b>.</Empty>
        : !d ? <Loading>Reading the bank book…</Loading> : (
        <>
          <div className="fin-stats">
            <Stat label="Opening balance" value={drcr(d.opening)} />
            <Stat label="Balance as per books" value={drcr(d.closing)} accent="money" />
            <Stat label="Deposits not yet in bank" value={'₹ ' + money(d.uncleared_debit)} />
            <Stat label="Cheques not yet presented" value={'₹ ' + money(d.uncleared_credit)} />
            <Stat label="Balance as per bank" value={drcr(d.bank_balance)} accent="count" />
          </div>
          {d.rows.length === 0 ? <Empty>No bank entries for these filters.</Empty> : (
            <div className="tablewrap fin-scroll"><table className="items fin-table">
              <thead><tr><th>Date</th><th>Voucher</th><th>Particulars</th><th>Cheque / Ref</th><th className="r">Deposit</th>
                <th className="r">Withdrawal</th><th style={{ width: 190 }}>Bank date</th></tr></thead>
              <tbody>{d.rows.map((r) => (
                <tr key={r.line_id} className={r.cleared_on ? 'fin-cleared' : ''}>
                  <td>{fmtDate(r.date)}</td><td className="mono">{r.voucher_no}<div className="small muted">{r.vtype}</div></td>
                  <td className="wrap">{r.narration}</td><td className="mono">{r.cheque_no || r.mode || ''}</td>
                  <td className="r">{amt(r.debit)}</td><td className="r">{amt(r.credit)}</td>
                  <td><div className="fin-inline">
                    <DateField inline value={r.cleared_on || ''} onChange={(v) => clear(r, v)} />
                    {!r.cleared_on && <button className="btn sm" title="Cleared on the voucher date" onClick={() => clear(r, r.date)}>✓</button>}
                  </div></td></tr>
              ))}</tbody>
            </table></div>
          )}
        </>
      )}
    </FinPage>
  )
}

// ==========================================================================
//  Cheque Book
// ==========================================================================
function ChequeBooks({ toast }) {
  const [o] = useFinOptions()
  const [bank, setBank] = useState('')
  const [books, setBooks] = useState(null)
  const [nb, setNb] = useState({ book_no: '', from_no: '', to_no: '', issued_on: todayIso() })
  const [busy, run] = useRun(toast)
  const load = () => api.finGet('cheque-books', { bank_id: bank }).then(setBooks).catch((e) => toast(errText(e), 'err'))
  useEffect(() => { load() }, [bank]) // eslint-disable-line react-hooks/exhaustive-deps
  const add = async () => {
    const r = await run(() => api.finSend('POST', 'cheque-books', { bank_ledger_id: +bank, book_no: nb.book_no,
      from_no: +nb.from_no, to_no: +nb.to_no, issued_on: nb.issued_on }), '✓ Cheque book added')
    if (r) { setNb({ book_no: '', from_no: '', to_no: '', issued_on: todayIso() }); load() }
  }
  const leaf = async (b, l) => {
    if (l.status === 'issued') return
    const undo = l.status === 'cancelled'
    const reason = undo ? '' : window.prompt(`Cancel leaf ${l.no}? Say why (spoiled, lost, stopped…)`, 'spoiled')
    if (!undo && reason === null) return
    await run(() => api.finSend('POST', `cheque-books/${b.id}/cancel`, { no: l.no, reason, undo }))
    load()
  }
  return (
    <FinPage title="Cheque Book" sub="Every leaf: issued, unused or cancelled"
      side={<>
        <F label="Bank"><Sel value={bank} onChange={setBank} placeholder="All banks"
          options={o.banks.map((b) => ({ value: String(b.id), label: b.name }))} /></F>
        <div className="fin-sidehead">Add a cheque book</div>
        {!bank ? <div className="small muted">Choose the bank it is drawn on.</div> : <>
          <F label="Book no"><input value={nb.book_no} onChange={(e) => setNb({ ...nb, book_no: e.target.value })} /></F>
          <div className="fin-range">
            <F label="First leaf"><input type="number" value={nb.from_no} onChange={(e) => setNb({ ...nb, from_no: e.target.value })} /></F>
            <F label="Last leaf"><input type="number" value={nb.to_no} onChange={(e) => setNb({ ...nb, to_no: e.target.value })} /></F>
          </div>
          <DateField label="Issued on" value={nb.issued_on} onChange={(v) => setNb({ ...nb, issued_on: v })} />
          <button className="btn primary block" disabled={busy || !nb.from_no || !nb.to_no} onClick={add}>
            <Icon name="plus" size={14} /> Add book</button>
        </>}
      </>}>
      {!books ? <Loading>Reading…</Loading> : books.length === 0 ? <Empty>No cheque books registered{bank ? ' for this bank' : ''}.</Empty> : books.map((b) => (
        <Section key={b.id} id={'chq.' + b.id} title={`${b.bank} · book ${b.book_no || b.id}`}
          summary={`${b.from_no}–${b.to_no} · ${b.used} issued · ${b.unused} unused · ${b.cancelled} cancelled`}>
          <div className="leafgrid">
            {b.leaves.map((l) => (
              <button key={l.no} className={'leaf ' + l.status} onClick={() => leaf(b, l)}
                title={l.status === 'issued' ? `${l.voucher_no} · ${fmtDate(l.date)} · ₹${money(l.amount)}${l.party ? ' · ' + l.party : ''}`
                  : l.status === 'cancelled' ? `Cancelled — ${l.reason}. Click to restore.` : 'Unused — click to cancel this leaf'}>
                <span className="no">{l.no}</span>
                <span className="st">{l.status === 'issued' ? '₹' + money(l.amount) : l.status}</span>
              </button>
            ))}
          </div>
        </Section>
      ))}
    </FinPage>
  )
}

// ==========================================================================
//  Petty Cash — a month sheet
// ==========================================================================
function PettyCash({ toast }) {
  const [o, reloadOpts] = useFinOptions()
  const [f, setF] = useState({ cash_ledger_id: '', business_id: '', location: '', month: todayIso().slice(0, 7) })
  const [sheet, setSheet] = useState(null)
  const [touched, setTouched] = useState(new Set())
  const [newLedger, setNewLedger] = useState(false)
  const [busy, run] = useRun(toast)
  const set = (k) => (v) => setF((p) => ({ ...p, [k]: v }))
  useEffect(() => { if (!f.cash_ledger_id && o.cash.length) set('cash_ledger_id')(String(o.cash[0].id)) }, [o.cash.length]) // eslint-disable-line react-hooks/exhaustive-deps
  const load = () => {
    if (!f.cash_ledger_id) return
    api.finGet('petty-cash', f).then((s) => { setSheet(s); setTouched(new Set()) }).catch((e) => toast(errText(e), 'err'))
  }
  useEffect(load, [f.cash_ledger_id, f.month, f.location]) // eslint-disable-line react-hooks/exhaustive-deps
  const cell = (ri, di, v) => {
    setSheet((s) => ({ ...s, rows: s.rows.map((r, i) => (i === ri ? { ...r, days: r.days.map((x, j) => (j === di ? v : x)) } : r)) }))
    setTouched((t) => new Set(t).add(di + 1))
  }
  const save = async () => {
    const r = await run(() => api.finSend('POST', 'petty-cash', { ...f, cash_ledger_id: +f.cash_ledger_id,
      business_id: f.business_id ? +f.business_id : null, touched: [...touched],
      rows: sheet.rows.map((r) => ({ ledger_id: r.ledger_id, days: r.days.map((x) => +x || 0) })) }),
    (x) => `✓ Saved — ${x.vouchers} day voucher(s) written`)
    if (r) load()
  }
  const dayTotals = sheet ? Array.from({ length: sheet.days }, (_, d) =>
    r2(sheet.rows.reduce((a, r) => a + (r.mode === 'Inc' ? 1 : -1) * (+r.days[d] || 0), 0))) : []
  return (
    <FinPage title="Petty Cash Management" sub={sheet ? `Opening ${drcr(sheet.opening)} · closing ${drcr(sheet.closing)}` : ''}
      actions={<>
        <button className="btn" onClick={() => setNewLedger(true)}><Icon name="plus" size={14} /> New Ledger</button>
        <button className="btn" disabled={!sheet} onClick={() => downloadCsv(`petty-cash-${f.month}`,
          [['particulars', 'Particulars'], ['mode', 'Mode'], ['total', 'Total'], ...Array.from({ length: sheet.days }, (_, i) => [(r) => r.days[i], String(i + 1)])],
          sheet.rows)}><Icon name="spreadsheet" size={14} /> Excel</button>
        <button className="btn primary" disabled={busy || !touched.size} onClick={save}>{busy ? 'Saving…' : `Save${touched.size ? ` (${touched.size} day${touched.size === 1 ? '' : 's'})` : ''}`}</button></>}
      side={<>
        <F label="Cash Ledger" req><Sel value={f.cash_ledger_id} onChange={set('cash_ledger_id')} placeholder="Choose"
          options={o.cash.map((c) => ({ value: String(c.id), label: c.name }))} /></F>
        <F label="Company"><Sel value={f.business_id} onChange={set('business_id')} placeholder="Default"
          options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <F label="Location"><Sel value={f.location} onChange={set('location')} placeholder="All locations" options={o.locations} /></F>
        <F label="Month" req><input type="month" value={f.month} onChange={(e) => set('month')(e.target.value)} /></F>
        <div className="small muted fin-note">Type each day's amount against its ledger. Income adds to the cash ledger, expense takes from it;
          saving writes one Petty Cash voucher per day changed.</div>
      </>}>
      {!f.cash_ledger_id ? <Empty>Choose the cash ledger this sheet draws on.</Empty> : !sheet ? <Loading>Reading…</Loading>
        : sheet.rows.length === 0 ? (
          <Empty>No petty-cash ledgers yet. Press <b>New Ledger</b> and give it a Petty Cash type
            ({o.petty_groups.join(', ')}).</Empty>
        ) : (
        <div className="tablewrap fin-scroll"><table className="items fin-table petty-grid">
          <thead><tr><th className="sticky">Particulars</th><th>Mode</th><th className="r">Total</th>
            {Array.from({ length: sheet.days }, (_, i) => <th key={i} className={'r' + (touched.has(i + 1) ? ' touched' : '')}>{i + 1}</th>)}</tr></thead>
          <tbody>{sheet.rows.map((r, ri) => (
            <tr key={r.ledger_id}><td className="sticky"><b>{r.particulars}</b><div className="small muted">{r.group}</div></td>
              <td><Badge tone={r.mode === 'Inc' ? 'confirmed' : 'pending'}>{r.mode}</Badge></td>
              <td className="r b">{money(r.days.reduce((a, x) => a + (+x || 0), 0))}</td>
              {r.days.map((v, di) => <td key={di}><input className="r" value={v || ''} inputMode="decimal"
                onChange={(e) => cell(ri, di, e.target.value)} /></td>)}</tr>
          ))}</tbody>
          <tfoot><tr><td className="sticky">Net for the day</td><td></td><td className="r">{money(dayTotals.reduce((a, x) => a + x, 0))}</td>
            {dayTotals.map((t, i) => <td key={i} className={'r ' + (t < 0 ? 'neg' : t > 0 ? 'pos' : '')}>{t ? money(t) : ''}</td>)}</tr></tfoot>
        </table></div>
      )}
      {newLedger && <LedgerForm init={{ group: o.petty_groups[1] || 'Petty Cash In-Direct Expense' }} toast={toast}
        onClose={() => setNewLedger(false)} onSaved={() => { setNewLedger(false); reloadOpts(); load() }} />}
    </FinPage>
  )
}

// ==========================================================================
//  Transport Payment / Agent Payment — one screen, two parties
// ==========================================================================
const PAYEE = {
  transport: {
    title: 'Transport Payment', party: 'Transport', listKey: 'transports', path: 'transport',
    stats: (t) => [['Freight', t.freight], ['Loading/Others', t.other], ['Total', t.total]],
    cols: [['transport', 'Transport'], ['supplier', 'Supplier'], ['lr_no', 'LR No'], ['entry_no', 'Entry No'],
      ['invoice_no', 'Invoice No'], ['date', 'Date', 'date'], ['other', 'Loading/Other', 'money'], ['freight', 'Freight', 'money'],
      ['total', 'Total', 'money'], ['paid', 'Paid', 'money']],
  },
  agent: {
    title: 'Agent Payment', party: 'Agent', listKey: 'agents', path: 'agent',
    stats: (t) => [['Commission', t.total]],
    cols: [['agent', 'Agent'], ['supplier', 'Supplier'], ['invoice_no', 'Invoice No'], ['date', 'Date', 'date'],
      ['receipt_no', 'Receipt No'], ['receipt_date', 'Date', 'date'], ['goods_value', 'Goods value', 'money'],
      ['rate', 'Comm', 'rate'], ['commission', 'Commission', 'money'], ['paid', 'Paid', 'money']],
  },
}

function PayeeScreen({ toast, kind }) {
  const cfg = PAYEE[kind]
  const [o] = useFinOptions()
  const [h, setH] = useState({ party: '', business_id: '', location: '', date_from: '', date_to: '', date: todayIso(),
    mode: 'NEFT', bank_ledger_id: '', cheque_no: '', cheque_date: '', ref_no: '', remarks: '' })
  const [rows, setRows] = useState(null)
  const [pick, setPick] = useState({})            // lr_id -> {on, amount, discount}
  const [tdsOn, setTdsOn] = useState(false)
  const [tdsPct, setTdsPct] = useState(1)
  const [busy, run] = useRun(toast)
  const set = (k) => (v) => setH((p) => ({ ...p, [k]: v?.target ? v.target.value : v }))
  const search = () => {
    setRows(null)
    api.finGet(`${cfg.path}/pending`, { [kind]: h.party, date_from: h.date_from, date_to: h.date_to })
      .then((r) => { setRows(r); setPick({}) }).catch((e) => { toast(errText(e), 'err'); setRows([]) })
  }
  useEffect(search, [kind]) // eslint-disable-line react-hooks/exhaustive-deps
  const chosen = (rows || []).filter((r) => pick[r.lr_id]?.on)
  const t = chosen.reduce((a, r) => {
    const p = pick[r.lr_id]
    a.amount += +p.amount || 0; a.discount += +p.discount || 0
    a.freight += r.freight || 0; a.other += r.other || 0; a.total += +p.amount || 0
    return a
  }, { amount: 0, discount: 0, freight: 0, other: 0, total: 0 })
  const tds = tdsOn ? r2(((t.amount - t.discount) * (+tdsPct || 0)) / 100) : 0
  const payable = r2(t.amount - t.discount - tds)
  const parties = new Set(chosen.map((r) => r[kind]))
  const toggle = (r, on) => setPick((p) => ({ ...p, [r.lr_id]: { on, amount: p[r.lr_id]?.amount ?? r.balance, discount: p[r.lr_id]?.discount ?? 0 } }))
  const allOn = rows?.length > 0 && rows.every((r) => pick[r.lr_id]?.on)
  const save = async () => {
    if (parties.size !== 1) { toast(`Tick rows for one ${cfg.party.toLowerCase()} at a time`, 'err'); return }
    const r = await run(() => api.finSend('POST', `${cfg.path}/pay`, { ...h, party: [...parties][0], tds,
      business_id: h.business_id ? +h.business_id : null, bank_ledger_id: h.bank_ledger_id ? +h.bank_ledger_id : null,
      rows: chosen.map((x) => ({ lr_id: x.lr_id, amount: +pick[x.lr_id].amount || 0, discount: +pick[x.lr_id].discount || 0 })) }),
    (v) => `✓ ${v.voucher_no} — ₹${money(payable)} paid to ${[...parties][0]}`)
    if (r) search()
  }
  return (
    <FinPage title={cfg.title} wideSide
      actions={<>
        <button className="btn" onClick={() => { setH((p) => ({ ...p, party: '' })); setPick({}) }}><Icon name="plus" size={14} /> New</button>
        <button className="btn primary" disabled={busy || !chosen.length} onClick={save}>{busy ? 'Saving…' : 'Save'}</button></>}
      side={<>
        <F label={cfg.party} req><div className="fin-inline">
          <Sel value={h.party} onChange={set('party')} placeholder={`All ${cfg.party.toLowerCase()}s`} options={o[cfg.listKey]} />
          <button className="btn primary icon" title="Search pending" onClick={search}><Icon name="search" size={14} /></button></div></F>
        <F label="Company"><Sel value={h.business_id} onChange={set('business_id')} placeholder="Default"
          options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <F label="Location"><Sel value={h.location} onChange={set('location')} placeholder="—" options={o.locations} /></F>
        <div className="fin-range">
          <DateField label="Received from" value={h.date_from} onChange={set('date_from')} />
          <DateField label="to" value={h.date_to} onChange={set('date_to')} />
        </div>
        <DateField label="Payment Date" required value={h.date} onChange={set('date')} />
        <F label="Payment Mode" req><Sel value={h.mode} allowBlank={false} onChange={set('mode')} options={o.modes} /></F>
        {h.mode !== 'Cash' && <F label="Bank" req><Sel value={h.bank_ledger_id} onChange={set('bank_ledger_id')} placeholder="Bank (Default)"
          options={o.banks.map((b) => ({ value: b.id, label: b.name }))} /></F>}
        {h.mode === 'Cheque' && <div className="fin-range">
          <F label="Cheque no"><input value={h.cheque_no} onChange={set('cheque_no')} /></F>
          <DateField label="Cheque Date" value={h.cheque_date} onChange={set('cheque_date')} /></div>}
        <F label="Ref. Nos"><input value={h.ref_no} onChange={set('ref_no')} placeholder="UTR / reference" /></F>
        <F label="Remarks"><input value={h.remarks} onChange={set('remarks')} /></F>
      </>}>
      <div className="fin-stats">
        {cfg.stats(t).map(([l, v]) => <Stat key={l} label={l} value={money(v)} />)}
        <Stat label="Discount" value={money(t.discount)} />
        <div className="stat fin-tds">
          <label className="lbl"><input type="checkbox" checked={tdsOn} onChange={(e) => setTdsOn(e.target.checked)} /> TDS</label>
          {tdsOn ? <div className="fin-inline"><input type="number" value={tdsPct} onChange={(e) => setTdsPct(e.target.value)} style={{ width: 60 }} />
            <span>% = ₹{money(tds)}</span></div> : <div className="val small">No TDS</div>}
        </div>
        <Stat label="Payable" value={'₹ ' + money(payable)} accent="money" />
      </div>
      {rows === null ? <Loading>Reading pending entries…</Loading> : rows.length === 0 ? (
        <Empty>Nothing pending{h.party ? ` for ${h.party}` : ''}. {kind === 'transport'
          ? 'Freight comes from LR Entry — an entry with freight charges shows here until it is paid.'
          : 'Commission comes from LR Entry — an entry with an agent and a commission shows here until it is paid.'}</Empty>
      ) : (
        <div className="tablewrap fin-scroll"><table className="items fin-table fin-entry">
          <thead><tr><th style={{ width: 40 }}>S.No</th>
            <th style={{ width: 34 }}><input type="checkbox" checked={allOn} onChange={() => rows.forEach((r) => toggle(r, !allOn))} /></th>
            <th className="r" style={{ width: 120 }}>Amount</th><th className="r" style={{ width: 100 }}>Discount</th>
            {cfg.cols.map(([k, l, ty]) => <th key={k} className={ty === 'money' || ty === 'rate' ? 'r' : ''}>{l}</th>)}
            <th className="r">Balance</th></tr></thead>
          <tbody>{rows.map((r, i) => {
            const p = pick[r.lr_id]
            return (
              <tr key={r.lr_id} className={p?.on ? 'fin-picked' : ''}>
                <td className="muted">{i + 1}</td>
                <td><input type="checkbox" checked={!!p?.on} onChange={(e) => toggle(r, e.target.checked)} /></td>
                <td><input className="r" type="number" disabled={!p?.on} value={p?.amount ?? ''}
                  onChange={(e) => setPick({ ...pick, [r.lr_id]: { ...p, amount: e.target.value } })} /></td>
                <td><input className="r" type="number" disabled={!p?.on} value={p?.discount ?? ''}
                  onChange={(e) => setPick({ ...pick, [r.lr_id]: { ...p, discount: e.target.value } })} /></td>
                {cfg.cols.map(([k, , ty]) => <td key={k} className={ty === 'money' || ty === 'rate' ? 'r' : ''}>
                  {ty === 'money' ? amt(r[k]) : ty === 'date' ? fmtDate(r[k]) : ty === 'rate' ? (r[k] <= 100 ? `${r[k]}%` : '₹' + money(r[k])) : (r[k] || '')}</td>)}
                <td className="r b">{money(r.balance)}</td>
              </tr>
            )
          })}</tbody>
          <tfoot><tr><td colSpan={2}>{chosen.length} ticked</td><td className="r">{money(t.amount)}</td><td className="r">{money(t.discount)}</td>
            <td colSpan={cfg.cols.length}></td><td className="r">{money((rows || []).reduce((a, r) => a + r.balance, 0))}</td></tr></tfoot>
        </table></div>
      )}
    </FinPage>
  )
}

// ==========================================================================
//  Credit Sale Collection
// ==========================================================================
function CreditCollection({ toast }) {
  const [o] = useFinOptions()
  const [debtors, reloadDebtors] = useLedgers({ group: 'Sundry Debtors' })
  const [h, setH] = useState({ ledger_id: '', business_id: '', location: '', date: todayIso(), mode: 'Cash',
    bank_ledger_id: '', ref_no: '', remarks: '', paid: '', discount: '' })
  const [tdsOn, setTdsOn] = useState(false)
  const [tds, setTds] = useState('')
  const [bills, setBills] = useState(null)
  const [alloc, setAlloc] = useState({})
  const [addCust, setAddCust] = useState(false)
  const [busy, run] = useRun(toast)
  const set = (k) => (v) => setH((p) => ({ ...p, [k]: v?.target ? v.target.value : v }))
  useEffect(() => {
    setBills(null); setAlloc({})
    if (h.ledger_id) api.finGet(`ledgers/${h.ledger_id}/bills`).then(setBills).catch(() => setBills({ bills: [], pending: 0 }))
  }, [h.ledger_id])
  const received = r2((+h.paid || 0) + (+h.discount || 0) + (tdsOn ? +tds || 0 : 0))
  const allocated = r2(Object.values(alloc).reduce((a, v) => a + (+v || 0), 0))
  const balance = bills ? r2(bills.pending - received) : 0
  // spread what was received over the oldest bills first
  const allocateDown = () => {
    let left = received
    const next = {}
    ;(bills?.bills || []).forEach((b) => { const x = Math.min(left, b.pending); if (x > 0) { next[b.bill_ref] = r2(x); left = r2(left - x) } })
    setAlloc(next)
  }
  const save = async () => {
    const r = await run(() => api.finSend('POST', 'collections', { ...h, ledger_id: +h.ledger_id,
      business_id: h.business_id ? +h.business_id : null, bank_ledger_id: h.bank_ledger_id ? +h.bank_ledger_id : null,
      paid: +h.paid || 0, discount: +h.discount || 0, tds: tdsOn ? +tds || 0 : 0,
      allocations: Object.entries(alloc).filter(([, v]) => +v).map(([bill_ref, amount]) => ({ bill_ref, amount: +amount })) }),
    (v) => `✓ Receipt ${v.voucher_no} saved`)
    if (r) setH((p) => ({ ...p, ledger_id: '', paid: '', discount: '', ref_no: '', remarks: '' }))
  }
  return (
    <FinPage title="Credit Sale Collection" wideSide
      actions={<>
        <button className="btn" onClick={() => setAddCust(true)}><Icon name="plus" size={14} /> New customer</button>
        <button className="btn primary" disabled={busy || !h.ledger_id || !received || allocated > received + 0.01} onClick={save}>
          {busy ? 'Saving…' : 'Save'}</button></>}
      side={<>
        <F label="B2B Customer" req><Sel value={h.ledger_id} onChange={set('ledger_id')} placeholder="Choose the customer"
          options={debtors.map((L) => ({ value: String(L.id), label: L.name }))} /></F>
        <F label="Company"><Sel value={h.business_id} onChange={set('business_id')} placeholder="Default"
          options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <F label="Location"><Sel value={h.location} onChange={set('location')} placeholder="—" options={o.locations} /></F>
        <div className="fin-bigfig"><span>Balance</span><b>{money(bills?.pending || 0)}</b></div>
        <div className="fin-bigfig"><span>After this receipt</span><b className={balance < 0 ? 'neg' : ''}>{money(balance)}</b></div>
        <div className="fin-sidehead">Payment details</div>
        <F label="Collected At"><Sel value={h.location} onChange={set('location')} placeholder="—" options={o.locations} /></F>
        <F label="Received By" req><Sel value={h.mode} allowBlank={false} onChange={set('mode')} options={o.modes} /></F>
        <DateField label="Payment Date" required value={h.date} onChange={set('date')} />
        {h.mode !== 'Cash' && <F label="Bank" req><Sel value={h.bank_ledger_id} onChange={set('bank_ledger_id')} placeholder="Bank (Default)"
          options={o.banks.map((b) => ({ value: b.id, label: b.name }))} /></F>}
        <F label="Ref no"><input value={h.ref_no} onChange={set('ref_no')} /></F>
        <F label="Remarks"><input value={h.remarks} onChange={set('remarks')} /></F>
      </>}>
      <div className="fin-stats">
        <div className="stat"><div className="lbl">Paid amount</div><div className="fin-inline">
          <input type="number" value={h.paid} onChange={set('paid')} placeholder="0.00" />
          <button className="btn primary icon" title="Allocate to the oldest bills" disabled={!bills} onClick={allocateDown}>↓</button></div></div>
        <div className="stat"><div className="lbl">Discount</div><input type="number" value={h.discount} onChange={set('discount')} placeholder="0.00" /></div>
        <div className="stat fin-tds"><label className="lbl"><input type="checkbox" checked={tdsOn} onChange={(e) => setTdsOn(e.target.checked)} /> TDS</label>
          {tdsOn ? <input type="number" value={tds} onChange={(e) => setTds(e.target.value)} placeholder="0.00" /> : <div className="val small">No TDS</div>}</div>
        <Stat label="Allocated" value={money(allocated)} accent={allocated > received + 0.01 ? 'warn' : undefined} />
        <Stat label="On account" value={money(Math.max(0, received - allocated))} />
      </div>
      {!h.ledger_id ? <Empty>Choose the customer to see what they owe.</Empty> : !bills ? <Loading>Reading…</Loading>
        : bills.bills.length === 0 ? <Empty>No pending bills — anything received goes on account.</Empty> : (
        <div className="tablewrap fin-scroll"><table className="items fin-table fin-entry">
          <thead><tr><th>Bill date</th><th>Voucher</th><th>Bill</th><th className="r">Days</th><th className="r">Amount</th>
            <th className="r">Settled</th><th className="r">Pending</th><th className="r" style={{ width: 140 }}>Allocate</th></tr></thead>
          <tbody>{bills.bills.map((b) => (
            <tr key={b.bill_ref} className={alloc[b.bill_ref] ? 'fin-picked' : ''}><td>{fmtDate(b.date)}</td><td className="mono">{b.voucher_no}</td>
              <td className="mono">{b.bill_ref}</td><td className="r">{b.days ?? ''}</td><td className="r">{money(b.amount)}</td>
              <td className="r">{amt(b.settled)}</td><td className="r b">{money(b.pending)}</td>
              <td><input className="r" type="number" value={alloc[b.bill_ref] ?? ''} onChange={(e) => setAlloc({ ...alloc, [b.bill_ref]: e.target.value })} /></td></tr>
          ))}</tbody>
        </table></div>
      )}
      {addCust && <LedgerForm init={{ group: 'Sundry Debtors', billwise: true }} lockGroup toast={toast} onClose={() => setAddCust(false)}
        onSaved={(L) => { setAddCust(false); reloadDebtors(); set('ledger_id')(String(L.id)) }} />}
    </FinPage>
  )
}

// ==========================================================================
//  Customer Credit Note
// ==========================================================================
const GST_RATES = [0, 5, 12, 18, 28]

function CreditNote({ toast }) {
  const [o] = useFinOptions()
  const [debtors, reloadDebtors] = useLedgers({ group: 'Sundry Debtors' })
  const [h, setH] = useState({ date: todayIso(), ledger_id: '', business_id: '', location: '', remarks: '', bill_ref: '', igst: false })
  const [bills, setBills] = useState([])
  const [rows, setRows] = useState([{ rate: 5, value: '' }])
  const [pct, setPct] = useState('')
  const [addCust, setAddCust] = useState(false)
  const [busy, run] = useRun(toast)
  const set = (k) => (v) => setH((p) => ({ ...p, [k]: v?.target ? (v.target.type === 'checkbox' ? v.target.checked : v.target.value) : v }))
  useEffect(() => {
    setBills([])
    if (h.ledger_id) api.finGet(`ledgers/${h.ledger_id}/bills`).then((b) => setBills(b.bills)).catch(() => {})
  }, [h.ledger_id])
  const bill = bills.find((b) => b.bill_ref === h.bill_ref)
  const calc = rows.map((r) => {
    const v = r2(r.value)
    const tax = r2((v * (+r.rate || 0)) / 100)
    return { ...r, tax, total: r2(v + tax) }
  })
  const total = r2(calc.reduce((a, r) => a + r.total, 0))
  // a percentage of the picked bill, split across the rows already there
  const applyPct = (p) => {
    setPct(p)
    if (!bill || !(+p)) return
    const target = r2((bill.pending * +p) / 100)
    const rate = +rows[0]?.rate || 0
    setRows([{ rate, value: r2(target / (1 + rate / 100)) }])
  }
  const save = async () => {
    const r = await run(() => api.finSend('POST', 'credit-notes', { ...h, ledger_id: +h.ledger_id,
      business_id: h.business_id ? +h.business_id : null, bill_no: bill?.voucher_no || null,
      rows: calc.map((x) => ({ rate: +x.rate || 0, value: +x.value || 0 })) }), (v) => `✓ Credit note ${v.voucher_no} saved`)
    if (r) { setRows([{ rate: 5, value: '' }]); setH((p) => ({ ...p, remarks: '', bill_ref: '' })); setPct('') }
  }
  return (
    <FinPage title="Customer Credit Note" wideSide
      actions={<>
        <button className="btn" onClick={() => setAddCust(true)}><Icon name="plus" size={14} /> New customer</button>
        <button className="btn primary" disabled={busy || !h.ledger_id || !total} onClick={save}>{busy ? 'Saving…' : 'Save'}</button></>}
      side={<>
        <DateField label="CR Date" required value={h.date} onChange={set('date')} />
        <F label="Customer" req><Sel value={h.ledger_id} onChange={set('ledger_id')} placeholder="Choose the customer"
          options={debtors.map((L) => ({ value: String(L.id), label: L.name }))} /></F>
        <F label="Company"><Sel value={h.business_id} onChange={set('business_id')} placeholder="Default"
          options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <F label="Location"><Sel value={h.location} onChange={set('location')} placeholder="—" options={o.locations} /></F>
        <F label="Remarks"><input value={h.remarks} onChange={set('remarks')} /></F>
        <F label="Bill No" hint={bill ? `Pending ₹${money(bill.pending)} on ${fmtDate(bill.date)}` : 'Optional — the bill this note reduces'}>
          <Sel value={h.bill_ref} onChange={set('bill_ref')} placeholder={h.ledger_id ? 'Against no bill' : 'Choose a customer first'}
            options={bills.map((b) => ({ value: b.bill_ref, label: `${b.voucher_no} · ₹${money(b.pending)}` }))} /></F>
        <F label="IGST sale"><label className="fin-check"><input type="checkbox" checked={h.igst} onChange={set('igst')} /> Inter-state (IGST)</label></F>
      </>}>
      <Section id="fin.cn.alloc" title="Allocation" summary={`Total : ₹${money(total)}`}>
        <div className="fin-toolbar flat">
          <F label="Percentage of the bill"><input type="number" value={pct} disabled={!bill} onChange={(e) => applyPct(e.target.value)} placeholder={bill ? '%' : 'pick a bill'} /></F>
          <F label="Value"><input readOnly value={money(total)} /></F>
        </div>
        <table className="items fin-table fin-entry">
          <thead><tr><th style={{ width: 110 }}>Tax %</th><th className="r">Value</th><th className="r">{h.igst ? 'IGST' : 'CGST + SGST'}</th>
            <th className="r">Adjusted</th><th style={{ width: 34 }}></th></tr></thead>
          <tbody>{calc.map((r, i) => (
            <tr key={i}>
              <td><select value={r.rate} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, rate: +e.target.value } : x)))}>
                {GST_RATES.map((g) => <option key={g} value={g}>{g}%</option>)}</select></td>
              <td><input className="r" type="number" value={r.value} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} /></td>
              <td className="r">{h.igst ? money(r.tax) : `${money(r.tax / 2)} + ${money(r.tax / 2)}`}</td>
              <td className="r b">{money(r.total)}</td>
              <td>{rows.length > 1 && <button className="rowdel" onClick={() => setRows(rows.filter((_, j) => j !== i))}>×</button>}</td></tr>
          ))}</tbody>
        </table>
        <button className="btn sm" style={{ marginTop: 10 }} onClick={() => setRows([...rows, { rate: 5, value: '' }])}><Icon name="plus" size={13} /> Add</button>
      </Section>
      {addCust && <LedgerForm init={{ group: 'Sundry Debtors', billwise: true }} lockGroup toast={toast} onClose={() => setAddCust(false)}
        onSaved={(L) => { setAddCust(false); reloadDebtors(); set('ledger_id')(String(L.id)) }} />}
    </FinPage>
  )
}

// ==========================================================================
//  Incentive Report — the store's staff commission
// ==========================================================================
function IncentiveReport({ toast }) {
  const [o] = useFinOptions()
  const [f, setF] = useState({ business_id: '', location: '', date_from: monthStart(), date_to: todayIso(), remove_zero: false })
  const [d, setD] = useState(null)
  const set = (k) => (v) => setF((p) => ({ ...p, [k]: v }))
  const load = () => { setD(null); api.finGet('incentive', f).then(setD).catch((e) => toast(errText(e), 'err')) }
  useEffect(load, []) // eslint-disable-line react-hooks/exhaustive-deps
  const cols = [['employee', 'Employee'], ['code', 'Code'], ['bills', 'Bills'], ['qty', 'Qty'], ['sales', 'Sales'], ['pct', '%'], ['incentive', 'Incentive']]
  return (
    <FinPage title="Incentive Process" sub="Each salesperson's commission on what they sold"
      actions={<>
        <button className="btn" disabled={!d?.rows.length} onClick={() => downloadCsv('incentive', cols, d.rows)}><Icon name="spreadsheet" size={14} /> CSV</button>
        <button className="btn" disabled={!d?.rows.length} onClick={() => window.print()}><Icon name="printer" size={14} /> Print</button>
        <button className="btn primary" onClick={load}><Icon name="search" size={14} /> Generate</button></>}
      side={<>
        <F label="Company"><Sel value={f.business_id} onChange={set('business_id')} placeholder="All companies"
          options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <F label="Location"><Sel value={f.location} onChange={set('location')} placeholder="All stores" options={o.locations} /></F>
        <DateRange from={f.date_from} to={f.date_to} setFrom={set('date_from')} setTo={set('date_to')} />
        <F label="Remove Zero"><label className="fin-check"><input type="checkbox" checked={f.remove_zero}
          onChange={(e) => set('remove_zero')(e.target.checked)} /> Hide staff with no incentive</label></F>
        <div className="small muted fin-note">Incentive is each person's commission % (Store → Staff) on the net value of the lines
          they sold. A line carries the staff ID scanned with it; otherwise the bill's.</div>
      </>}>
      {!d ? <Loading>Reading the till…</Loading> : !d.available ? <Empty>The store's database is not connected here, so there are no sales to pay on.</Empty> : (
        <>
          <div className="fin-stats"><Stat label="Sales" value={'₹ ' + money(d.totals.sales)} />
            <Stat label="Pieces" value={money(d.totals.qty)} /><Stat label="Incentive" value={'₹ ' + money(d.totals.incentive)} accent="money" /></div>
          {d.rows.length === 0 ? <Empty>No staff sales in this period.</Empty> : (
            <div className="tablewrap fin-scroll"><table className="items fin-table">
              <thead><tr><th>Employee</th><th>Code</th><th className="r">Bills</th><th className="r">Qty</th><th className="r">Sales</th>
                <th className="r">%</th><th className="r">Incentive</th></tr></thead>
              <tbody>{d.rows.map((r) => (
                <tr key={r.staff_id}><td><b>{r.employee}</b></td><td className="mono">{r.code}</td><td className="r">{r.bills}</td>
                  <td className="r">{r.qty}</td><td className="r">{money(r.sales)}</td><td className="r">{r.pct ? r.pct + '%' : <span className="muted">not set</span>}</td>
                  <td className="r b">{money(r.incentive)}</td></tr>
              ))}</tbody>
            </table></div>
          )}
        </>
      )}
    </FinPage>
  )
}

// ==========================================================================
//  Tally Configuration and Tally Integration
// ==========================================================================
const TALLY_TABS = [['purchase', 'Purchase'], ['sales', 'Sales'], ['voucher_types', 'Voucher Types'], ['payment_modes', 'Payment Modes']]
const TALLY_KEYS = {
  purchase: [['purchase', 'Purchase Accounts'], ['purchase_ret', 'Purchase Returns'], ['input_gst', 'Input GST'], ['freight', 'Freight Inward'],
    ['disc_recv', 'Discount Received'], ['tds', 'TDS Payable'], ['rounding', 'Rounding Off']],
  sales: [['sales', 'Sales Accounts'], ['sales_ret', 'Sales Returns'], ['output_gst', 'Output GST'], ['disc_allow', 'Discount Allowed'],
    ['card', 'Card Settlement'], ['upi', 'UPI Settlement'], ['cash', 'Cash']],
}

function TallyConfiguration({ toast }) {
  const [o] = useFinOptions()
  const [biz, setBiz] = useState(null)
  const [cfg, setCfg] = useState(null)
  const [tab, setTab] = useState('purchase')
  const [busy, run] = useRun(toast)
  useEffect(() => {
    if (!biz) return
    setCfg(null)
    api.finGet('tally/config', { business_id: biz.id }).then(setCfg).catch((e) => toast(errText(e), 'err'))
  }, [biz, toast])
  const map = (kind) => (cfg?.mapping || {})[kind] || {}
  const setMap = (kind, k, v) => setCfg({ ...cfg, mapping: { ...cfg.mapping, [kind]: { ...map(kind), [k]: v } } })
  const save = () => run(() => api.finSend('PUT', 'tally/config', { ...cfg, business_id: biz.id, port: +cfg.port || 9000 }), '✓ Tally configuration saved')
  return (
    <FinPage title="Tally Configuration" sub="Where each company's Tally runs, and what its ledgers are called there">
      <div className="fin-split">
        <Section id="fin.tally.companies" title="Company list">
          <table className="items fin-table">
            <thead><tr><th style={{ width: 50 }}>SNO</th><th>Code</th><th>Name</th><th></th></tr></thead>
            <tbody>{o.businesses.map((b, i) => (
              <tr key={b.id} className={biz?.id === b.id ? 'fin-picked' : ''}><td>{i + 1}</td><td className="mono">{b.code}</td><td>{b.name}</td>
                <td className="r"><button className="btn sm success" onClick={() => setBiz(b)}>Configure</button></td></tr>
            ))}</tbody>
          </table>
        </Section>
        <div>
          {!biz ? <Empty>Press <b>Configure</b> beside a company.</Empty> : !cfg ? <Loading>Reading…</Loading> : (
            <Section id="fin.tally.server" title={`Tally Server — ${biz.name}`}
              actions={<button className="btn primary sm" disabled={busy} onClick={save}>{busy ? 'Saving…' : 'Save'}</button>}>
              <div className="fin-form three">
                <F label="Company (as named in Tally)"><input value={cfg.tally_company || ''} onChange={(e) => setCfg({ ...cfg, tally_company: e.target.value })} /></F>
                <F label="Computer" hint="IP or name of the machine running Tally"><input value={cfg.host || ''} placeholder="192.168.1.20"
                  onChange={(e) => setCfg({ ...cfg, host: e.target.value })} /></F>
                <F label="Port"><input type="number" value={cfg.port || ''} placeholder="9000" onChange={(e) => setCfg({ ...cfg, port: e.target.value })} /></F>
              </div>
              <div className="fin-sidehead" style={{ marginTop: 16 }}>Select — Tally Config</div>
              <div className="subtabs fin-subtabs" role="tablist">
                {TALLY_TABS.map(([k, l]) => <button key={k} role="tab" aria-selected={tab === k} className={'subtab' + (tab === k ? ' on' : '')}
                  onClick={() => setTab(k)}>{l}</button>)}
              </div>
              <table className="items fin-table fin-entry">
                <thead><tr><th>{tab === 'voucher_types' ? 'Voucher type here' : tab === 'payment_modes' ? 'Payment mode' : 'Ledger here'}</th>
                  <th>Name in Tally</th></tr></thead>
                <tbody>
                  {(tab === 'voucher_types' ? o.vtypes.map((v) => [v, v]) : tab === 'payment_modes' ? o.modes.map((m) => [m, m])
                    : TALLY_KEYS[tab]).map(([k, l]) => {
                    // the auto-posting ledgers are keyed by their fixed role, so a
                    // rename here does not lose the Tally name
                    const kind = tab === 'voucher_types' || tab === 'payment_modes' ? tab : 'system'
                    const key = k
                    return (
                      <tr key={k}><td>{l}</td><td><input value={map(kind)[key] || ''} placeholder={l}
                        onChange={(e) => setMap(kind, key, e.target.value)} /></td></tr>
                    )
                  })}
                </tbody>
              </table>
              <div className="small muted" style={{ marginTop: 8 }}>Blank means the same name is used in Tally. Every other ledger is sent under its own name.</div>
            </Section>
          )}
        </div>
      </div>
    </FinPage>
  )
}

function TallyIntegration({ toast }) {
  const [o] = useFinOptions()
  const [f, setF] = useState({ vtype: 'Purchase', business_id: '', date_from: monthStart(), date_to: todayIso(), party: '' })
  const [rows, setRows] = useState(null)
  const [cfg, setCfg] = useState(null)
  const [ticked, setTicked] = useState(new Set())
  const [cur, setCur] = useState(null)
  const [busy, run] = useRun(toast)
  const set = (k) => (v) => setF((p) => ({ ...p, [k]: v }))
  const load = () => {
    setRows(null); setTicked(new Set()); setCur(null)
    api.finGet('vouchers', { vtype: f.vtype, date_from: f.date_from, date_to: f.date_to, business_id: f.business_id })
      .then((r) => setRows(r.filter((v) => v.status !== 'cancelled' && (!f.party || (v.party || '').toLowerCase().includes(f.party.toLowerCase())))))
      .catch((e) => { toast(errText(e), 'err'); setRows([]) })
    api.finGet('tally/config', { business_id: f.business_id }).then(setCfg).catch(() => setCfg(null))
  }
  useEffect(load, []) // eslint-disable-line react-hooks/exhaustive-deps
  const chosen = (rows || []).filter((v) => ticked.has(v.id))
  const total = r2(chosen.reduce((a, v) => a + v.amount, 0))
  const allOn = rows?.length > 0 && rows.every((v) => ticked.has(v.id))
  const tick = (id) => setTicked((t) => { const n = new Set(t); if (n.has(id)) n.delete(id); else n.add(id); return n })
  const push = async () => {
    const r = await run(() => api.finSend('POST', 'tally/push', { business_id: f.business_id ? +f.business_id : null, voucher_ids: [...ticked] }))
    if (r) { toast(r.status === 'ok' ? `✓ Tally took ${r.created} voucher(s)` : `Tally reported ${r.errors} error(s) — check the ledgers exist there`, r.status === 'ok' ? 'ok' : 'err'); load() }
  }
  const xml = () => window.open(`/api/finance/tally/xml?ids=${[...ticked].join(',')}${f.business_id ? '&business_id=' + f.business_id : ''}`, '_blank')
  return (
    <FinPage title="Tally Integration" sub={cfg?.host ? `Tally at ${cfg.host}:${cfg.port}${cfg.tally_company ? ' · ' + cfg.tally_company : ''}` : 'Tally not configured yet'}
      actions={<>
        <button className="btn" onClick={load}><Icon name="search" size={14} /> Search</button>
        <button className="btn" disabled={!ticked.size} onClick={xml}><Icon name="filetext" size={14} /> Download XML</button>
        <button className="btn primary" disabled={busy || !ticked.size || !cfg?.host} title={cfg?.host ? '' : 'Set the machine and port on Tally Configuration'}
          onClick={push}>{busy ? 'Sending…' : `Transfer${ticked.size ? ` (${ticked.size})` : ''}`}</button></>}
      side={<>
        <F label="Report Type"><Sel value={f.vtype} allowBlank={false} onChange={set('vtype')} options={o.vtypes} /></F>
        <F label="Company"><Sel value={f.business_id} onChange={set('business_id')} placeholder="Default"
          options={o.businesses.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <DateRange from={f.date_from} to={f.date_to} setFrom={set('date_from')} setTo={set('date_to')} />
        <F label="Party"><input value={f.party} onChange={(e) => set('party')(e.target.value)} placeholder="Supplier / customer" /></F>
        <F label="Tally Company"><input readOnly value={cfg?.tally_company || ''} placeholder="—" /></F>
        <F label="Machine:Port"><input readOnly value={cfg?.host ? `${cfg.host}:${cfg.port}` : ''} placeholder="—" /></F>
        <div className="fin-bigfig"><span>Total Value</span><b>{money(total)}</b></div>
      </>}>
      <div className="fin-split wide">
        <div>
          {rows === null ? <Loading>Reading…</Loading> : rows.length === 0 ? <Empty>No {f.vtype} vouchers in this period.</Empty> : (
            <div className="tablewrap fin-scroll"><table className="items fin-table">
              <thead><tr><th style={{ width: 34 }}><input type="checkbox" checked={allOn}
                onChange={() => setTicked(allOn ? new Set() : new Set(rows.map((v) => v.id)))} /></th>
                <th>SNO</th><th>Date</th><th>NO</th><th>Ledger</th><th className="r">Amount</th><th>Tally</th></tr></thead>
              <tbody>{rows.map((v, i) => (
                <tr key={v.id} className={'clickable' + (cur?.id === v.id ? ' fin-picked' : '')} onClick={() => setCur(v)}>
                  <td onClick={(e) => e.stopPropagation()}><input type="checkbox" checked={ticked.has(v.id)} onChange={() => tick(v.id)} /></td>
                  <td className="muted">{i + 1}</td><td>{fmtDate(v.date)}</td><td className="mono">{v.voucher_no}</td>
                  <td>{v.party || v.lines?.[0]?.ledger}</td><td className="r">{money(v.amount)}</td>
                  <td>{v.tally_status ? <Badge tone={v.tally_status === 'ok' ? 'confirmed' : 'cancelled'}
                    title={v.tally_synced_at || ''}>{v.tally_status === 'ok' ? 'sent' : 'error'}</Badge> : <span className="muted small">not sent</span>}</td></tr>
              ))}</tbody>
            </table></div>
          )}
        </div>
        <div>
          <Section id="fin.tally.entry" title="Entry" summary={cur ? cur.voucher_no : 'Click a voucher'}>
            {!cur ? <div className="small muted">The ledger lines Tally will receive show here.</div> : (
              <>
                <table className="items fin-table"><thead><tr><th>Ledger</th><th className="r">Debit</th><th className="r">Credit</th></tr></thead>
                  <tbody>{cur.lines.map((l) => <tr key={l.id}><td>{l.ledger}</td><td className="r">{amt(l.debit)}</td><td className="r">{amt(l.credit)}</td></tr>)}</tbody></table>
                <F label="Narration" style={{ marginTop: 10 }}><textarea rows={3} readOnly value={cur.narration || ''} /></F>
              </>
            )}
          </Section>
        </div>
      </div>
    </FinPage>
  )
}

// ==========================================================================
//  The module: which screen a menu key opens
// ==========================================================================
//: key, label, blurb, icon — read by App for the menu, the rail and the
//: category page, in the order the reference menu lists them.
// The parts every Finance screen is drawn from, shared with HR (HR.jsx) so the
// two office modules read as one app: the same page band, filter column,
// pickers, dialogs and badges.
export { fyStart, r2 }

export const FIN_SCREENS = [
  ['fin_transport', 'Transport Payment', 'Pay transporters the freight on LR entries', 'truck'],
  ['fin_agent', 'Agent Payment', 'Pay agents their commission on LR entries', 'usercheck'],
  ['fin_collection', 'Credit Sale Collection', 'Receive from B2B customers, bill by bill', 'rupee'],
  ['fin_creditnote', 'Credit Note', 'Customer credit notes, by GST rate', 'undo'],
  ['fin_gst', 'GST Inbox', 'Input tax on every purchase, checked', 'filetext'],
  ['fin_ledgers', 'Manage Ledger', 'The chart of accounts', 'book'],
  ['fin_vouchers', 'Voucher Entry', 'Payments, receipts, journals and contra', 'receipt'],
  ['fin_petty', 'Petty Cash', 'A month sheet of small cash spends', 'card'],
  ['fin_dailypl', 'Daily P&L', 'What each day earned and spent', 'trending'],
  ['fin_cheques', 'Cheque Book', 'Every leaf: issued, unused, cancelled', 'layers'],
  ['fin_brs', 'BRS Book', 'Bank reconciliation', 'building'],
  ['fin_payable', 'Payable', 'What we owe, aged', 'outward'],
  ['fin_receivable', 'Receivable', 'What we are owed, aged', 'inward'],
  ['fin_daybook', 'Daybook', 'Every voucher, day by day', 'history'],
  ['fin_tds', 'TDS Transactions', 'Tax deducted at source', 'percent'],
  ['fin_analyser', 'Analyser', 'Slice the books by location, type and ledger', 'calc'],
  ['fin_trial', 'Trial Balance', 'Every ledger, debit against credit', 'spreadsheet'],
  ['fin_trading', 'Trading Account', 'Gross profit', 'trending'],
  ['fin_pl', 'Profit & Loss Account', 'Net profit', 'trending'],
  ['fin_bs', 'Balance Sheet', 'What the business owns and owes', 'shield'],
  ['fin_loans', 'Loan Reconciliation', 'Loans taken and given', 'undo'],
  ['fin_incentive', 'Incentive Report', 'Staff commission on store sales', 'sparkle'],
  ['fin_tally', 'Tally Integration', 'Send vouchers to Tally', 'upload'],
  ['fin_tallycfg', 'Tally Configuration', 'Where Tally runs, and its ledger names', 'sliders'],
  ['fin_opening', 'Opening Balance', 'What every ledger held on day one', 'rupee'],
]

//: The Finance screens that are reports — they read the books and change
//: nothing — listed again under Reports, in the order a month-end is read.
export const FIN_REPORT_KEYS = ['fin_daybook', 'fin_trial', 'fin_trading', 'fin_pl', 'fin_bs', 'fin_dailypl',
  'fin_payable', 'fin_receivable', 'fin_analyser', 'fin_tds', 'fin_gst', 'fin_loans', 'fin_incentive']

export default function Finance({ screen, toast }) {
  switch (screen) {
    case 'fin_transport': return <PayeeScreen key="t" kind="transport" toast={toast} />
    case 'fin_agent': return <PayeeScreen key="a" kind="agent" toast={toast} />
    case 'fin_collection': return <CreditCollection toast={toast} />
    case 'fin_creditnote': return <CreditNote toast={toast} />
    case 'fin_gst': return <GstInbox toast={toast} />
    case 'fin_ledgers': return <ManageLedger toast={toast} />
    case 'fin_vouchers': return <VoucherEntry toast={toast} />
    case 'fin_petty': return <PettyCash toast={toast} />
    case 'fin_dailypl': return <DailyPL toast={toast} />
    case 'fin_cheques': return <ChequeBooks toast={toast} />
    case 'fin_brs': return <BankReconciliation toast={toast} />
    case 'fin_payable': return <Outstanding key="p" side="payable" toast={toast} />
    case 'fin_receivable': return <Outstanding key="r" side="receivable" toast={toast} />
    case 'fin_daybook': return <Daybook toast={toast} />
    case 'fin_tds': return <TdsTransactions toast={toast} />
    case 'fin_analyser': return <Analyser toast={toast} />
    case 'fin_trial': return <TrialBalance toast={toast} />
    case 'fin_trading': return <TradingAccount toast={toast} />
    case 'fin_pl': return <ProfitLoss toast={toast} />
    case 'fin_bs': return <BalanceSheet toast={toast} />
    case 'fin_loans': return <LoanReconciliation toast={toast} />
    case 'fin_incentive': return <IncentiveReport toast={toast} />
    case 'fin_tally': return <TallyIntegration toast={toast} />
    case 'fin_tallycfg': return <TallyConfiguration toast={toast} />
    case 'fin_opening': return <OpeningBalance toast={toast} />
    default: return <Empty>Unknown Finance screen.</Empty>
  }
}
