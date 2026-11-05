// ==========================================================================
//  HR — people, time, leave and pay
//  ------------------------------------------------------------------------
//  Fourteen screens over one engine (backend services/hr.py). An employee is
//  one record from joining to leaving; every day of theirs is one attendance
//  row, whoever wrote it (a supervisor, a biometric file, a device, the till's
//  check-in); leave, overtime and payroll are read off those rows; and an
//  approved payroll posts itself into Finance's books.
//
//  Drawn from the same parts as Finance (FinPage, F, Sel, Modal, Badge…) so
//  the two office modules read as one app. Dates are typed and shown
//  DD-MM-YYYY and sent ISO; a month is YYYY-MM.
// ==========================================================================
import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from './api.js'
import './hr.css'
import { Hourglass, Loading } from './Loader.jsx'
import { Stat, DateField, money, fmtDate, Icon } from './App.jsx'
import {
  FinPage, F, Sel, CheckList, DateRange, Empty, Modal, Badge, useRun, downloadCsv, errText,
  monthStart, todayIso,
} from './hr-kit.jsx'

// ---------------------------------------------------------------- helpers
const rs = (v) => '₹ ' + money(v)
const rs0 = (v) => '₹' + Math.round(+v || 0).toLocaleString('en-IN')
const num = (v) => (v === '' || v == null ? '' : +v)
const thisMonth = () => todayIso().slice(0, 7)
function lastMonth() {
  const d = new Date()
  d.setDate(1)
  d.setMonth(d.getMonth() - 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}
const monthName = (m) => {
  if (!m) return ''
  const [y, mm] = m.split('-')
  return new Date(+y, +mm - 1, 1).toLocaleString('en-IN', { month: 'long', year: 'numeric' })
}
const initials = (n) => String(n || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join('')
const hrs = (min) => (min ? String(Math.round(min / 6) / 10) : '')
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`
const people = (n) => `${n} ${n === 1 ? 'person' : 'people'}`
const ST_NAME = { P: 'Present', HD: 'Half day', A: 'Absent', L: 'Leave', WO: 'Weekly off', H: 'Holiday', OD: 'On duty' }
const STATUS_TONE = { active: 'confirmed', notice: 'pending', exited: 'cancelled', draft: 'draft', approved: 'posted',
  paid: 'confirmed', pending: 'pending', rejected: 'cancelled', cancelled: 'cancelled', open: 'pending', closed: 'draft',
  submitted: 'pending', applied: 'confirmed' }
const Tone = ({ s, children }) => <Badge tone={STATUS_TONE[s] || 'draft'}>{children || s}</Badge>

function HrPage(props) {
  return <FinPage crumb="HR" {...props} />
}

// Indian numbering, for the payslip's "net pay in words".
function inWords(v) {
  let n = Math.round(+v || 0)
  if (!n) return 'Zero'
  const a = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten', 'Eleven', 'Twelve',
    'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen']
  const b = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']
  const two = (x) => (x < 20 ? a[x] : b[Math.floor(x / 10)] + (x % 10 ? ' ' + a[x % 10] : ''))
  const three = (x) => (x >= 100 ? a[Math.floor(x / 100)] + ' Hundred' + (x % 100 ? ' ' : '') : '') + (x % 100 ? two(x % 100) : '')
  const parts = []
  const crore = Math.floor(n / 1e7); n %= 1e7
  const lakh = Math.floor(n / 1e5); n %= 1e5
  const th = Math.floor(n / 1000); n %= 1000
  if (crore) parts.push(three(crore) + ' Crore')
  if (lakh) parts.push(two(lakh) + ' Lakh')
  if (th) parts.push(two(th) + ' Thousand')
  if (n) parts.push(three(n))
  return parts.join(' ')
}

// A print of its own, in a window of its own: payslips and reports print as the
// document they are, not as the screen around them.
const PRINT_CSS = `body{font:12px/1.45 system-ui,Segoe UI,Arial,sans-serif;color:#1f2937;margin:24px}
h1{font-size:18px;margin:0}h2{font-size:14px;margin:14px 0 6px}table{border-collapse:collapse;width:100%}
th,td{border:1px solid #d1d5db;padding:5px 8px;text-align:left;vertical-align:top}th{background:#f3f4f6}
td.r,th.r{text-align:right}.muted{color:#6b7280}.slip{page-break-after:always;max-width:780px;margin:0 auto 24px}
.slip:last-child{page-break-after:auto}.hd{display:flex;justify-content:space-between;align-items:flex-end;
border-bottom:2px solid #2F8F68;padding-bottom:8px;margin-bottom:10px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
.kv{display:flex;justify-content:space-between;border-bottom:1px dashed #e5e7eb;padding:3px 0}.net{margin-top:10px;padding:10px;
background:#ecfdf5;border:1px solid #a7f3d0;display:flex;justify-content:space-between;font-weight:700;font-size:14px}
.foot{margin-top:18px;font-size:10px;color:#6b7280}`

function printHtml(title, html) {
  const w = window.open('', '_blank', 'width=900,height=1000')
  if (!w) { window.alert('Allow pop-ups for this site to print'); return }
  w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${esc(title)}</title><style>${PRINT_CSS}</style></head>`
    + `<body>${html}<script>window.onload=function(){window.print()}<\/script></body></html>`)
  w.document.close()
}

// ---------------------------------------------------------------- shared data
let optCache = null
let optWaiters = []
function loadOptions(force) {
  if (optCache && !force) return Promise.resolve(optCache)
  return api.hrGet('options').then((o) => { optCache = o; optWaiters.forEach((f) => f(o)); return o })
}
const BLANK_OPTS = { doc_groups: [], departments: [], designations: [], shifts: [], leave_types: [], employees: [], locations: [],
  banks: [], components: [], employment_types: [], exit_types: [], doc_types: [], contract_types: [],
  history_kinds: [], weekdays: [], statuses: {}, genders: [], pay_modes: [], incentive_bases: [], goal_metrics: [],
  store: false }
function useHrOptions() {
  const [o, setO] = useState(optCache)
  useEffect(() => {
    let live = true
    const take = (x) => { if (live) setO(x) }
    optWaiters.push(take)
    loadOptions().then(take).catch(() => {})
    return () => { live = false; optWaiters = optWaiters.filter((f) => f !== take) }
  }, [])
  return [o || BLANK_OPTS, () => loadOptions(true)]
}

const empOpts = (o) => o.employees.map((e) => ({ value: e.id, label: `${e.name} · ${e.code}` }))

function Avatar({ id, name, photo, size = 32 }) {
  const [bad, setBad] = useState(false)
  const style = { width: size, height: size, fontSize: Math.round(size * 0.38) }
  return photo && !bad
    ? <img className="hr-av" style={style} src={`/api/hr/employees/${id}/photo?v=${photo}`} alt="" onError={() => setBad(true)} />
    : <span className="hr-av" style={style} aria-hidden="true">{initials(name)}</span>
}

function MonthField({ label = 'Month', value, onChange }) {
  return <F label={label}><input type="month" value={value} onChange={(e) => e.target.value && onChange(e.target.value)} /></F>
}

function Tabs({ tabs, value, onChange }) {
  return (
    <div className="subtabs fin-subtabs hr-tabs" role="tablist">
      {tabs.filter(Boolean).map(([k, l, n]) => (
        <button key={k} role="tab" aria-selected={value === k} className={'subtab' + (value === k ? ' on' : '')}
          onClick={() => onChange(k)}>{l}{n ? <span className="hr-count">{n}</span> : null}</button>
      ))}
    </div>
  )
}

function Card({ title, action, children, wide, tall }) {
  return (
    <section className={'hr-card' + (wide ? ' wide' : '') + (tall ? ' tall' : '')}>
      <div className="hr-cardhead"><h4>{title}</h4>{action}</div>
      <div className="hr-cardbody">{children}</div>
    </section>
  )
}

function Bars({ rows, fmt = (v) => v, tone }) {
  const max = Math.max(1, ...rows.map((r) => +r[1] || 0))
  return (
    <div className="hr-bars">
      {rows.map(([label, v]) => (
        <div key={label} className="hr-barrow">
          <span className="hr-barlabel" title={label}>{label}</span>
          <span className="hr-bartrack"><i className={tone} style={{ width: `${Math.max(2, (+v || 0) / max * 100)}%` }} /></span>
          <b>{fmt(v)}</b>
        </div>
      ))}
    </div>
  )
}

// a form's two-way binding, for inputs, selects and ticks alike
const binder = (setF) => (k) => (v) => setF((p) => ({ ...p, [k]: v?.target ? (v.target.type === 'checkbox' ? v.target.checked : v.target.value) : v }))

// A select you can type into. Typing narrows the list; given onCreate it also
// offers to add what was typed (onCreate returns the new value, or null when it
// takes over, say by opening a form). Given onEdit, a pencil beside it edits the
// chosen entry. Options are strings or { value, label, sub }.
function Combo({ value, onChange, options, blankLabel = '—', onCreate, noun = '', onEdit, disabled, emptyNote }) {
  const opts = useMemo(() => (options || []).map((o) => (typeof o === 'object' ? o : { value: o, label: o })), [options])
  const cur = opts.find((o) => String(o.value) === String(value ?? ''))
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [hi, setHi] = useState(0)
  const [busy, setBusy] = useState(false)
  const [pos, setPos] = useState(null)
  const input = useRef(null)
  const list = useRef(null)
  const id = useId()
  const t = q.trim().toLowerCase()
  const hits = t ? opts.filter((o) => `${o.label} ${o.sub || ''}`.toLowerCase().includes(t)) : opts
  const exact = t && opts.some((o) => String(o.label).toLowerCase() === t)
  const items = [
    ...(t ? [] : [{ value: '', label: blankLabel, blank: true }]),
    ...hits,
    ...(onCreate && t && !exact ? [{ create: true, label: q.trim() }] : []),
  ]
  const place = useCallback(() => {
    const r = input.current?.getBoundingClientRect()
    if (!r) return
    const below = window.innerHeight - r.bottom
    setPos(below < 240 && r.top > below
      ? { left: r.left, width: r.width, bottom: window.innerHeight - r.top + 4 }
      : { left: r.left, width: r.width, top: r.bottom + 4 })
  }, [])
  const show = () => { if (disabled) return; setQ(''); setHi(0); place(); setOpen(true) }
  const hide = () => { setOpen(false); setQ('') }
  useEffect(() => {
    if (!open) return undefined
    const away = (ev) => { if (!input.current?.parentNode.contains(ev.target) && !list.current?.contains(ev.target)) hide() }
    document.addEventListener('mousedown', away)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => { document.removeEventListener('mousedown', away); window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true) }
  }, [open, place])
  useEffect(() => { list.current?.querySelector('[aria-selected=true]')?.scrollIntoView({ block: 'nearest' }) }, [hi, open])
  const pick = async (it) => {
    if (!it) return
    hide()
    if (!it.create) { onChange(String(it.value)); return }
    setBusy(true)
    try {
      const v = await onCreate(it.label)
      if (v != null) onChange(String(v))
    } finally { setBusy(false) }
  }
  const key = (ev) => {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault()
      if (!open) { show(); return }
      setHi((h) => (h + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length)
    } else if (ev.key === 'Enter' && open) {
      ev.preventDefault(); pick(items[hi])
    } else if (ev.key === 'Escape' && open) {
      ev.stopPropagation(); hide()
    } else if (ev.key === 'Tab') hide()
  }
  return (
    <div className={'hr-combo' + (onEdit && cur ? ' editable' : '')}>
      <input ref={input} role="combobox" aria-expanded={open} aria-controls={id} aria-autocomplete="list"
        aria-activedescendant={open && items[hi] ? `${id}-${hi}` : undefined} disabled={disabled || busy}
        value={open ? q : busy ? 'Adding…' : cur?.label || ''} placeholder={open ? (cur?.label || 'Type to search') : blankLabel}
        onFocus={show} onClick={() => !open && show()} onChange={(ev) => { setQ(ev.target.value); setHi(0); if (!open) { place(); setOpen(true) } }}
        onKeyDown={key} />
      <Icon name="chevron" size={14} className="hr-combocaret" />
      {onEdit && cur && <button type="button" className="hr-comboedit" disabled={disabled} title={`Edit ${cur.label}`}
        aria-label={`Edit ${cur.label}`} onClick={() => onEdit(cur)}><Icon name="pencil" size={13} /></button>}
      {open && pos && createPortal(
        <ul ref={list} id={id} role="listbox" className="hr-combolist" style={pos} onMouseDown={(ev) => ev.preventDefault()}>
          {items.map((it, i) => (
            <li key={it.create ? '+new' : `v${it.value}`} id={`${id}-${i}`} role="option" aria-selected={i === hi}
              className={(it.blank ? 'blank' : '') + (it.create ? ' create' : '') + (!it.create && !it.blank && String(it.value) === String(value ?? '') ? ' cur' : '')}
              onMouseEnter={() => setHi(i)} onClick={() => pick(it)}>
              {it.create ? <><Icon name="plus" size={13} /> Add {noun} “{it.label}”</> : <><span>{it.label}</span>{it.sub && <small>{it.sub}</small>}</>}
            </li>
          ))}
          {!hits.length && t && !onCreate && <li className="none">Nothing matches “{q.trim()}”</li>}
          {!opts.length && !t && emptyNote && <li className="none">{emptyNote}</li>}
        </ul>, document.body)}
    </div>
  )
}

// The lists an employee's job is picked from, and how each is edited — shared
// by HR Settings, Shifts and the employee form, so all three edit them alike.
const SHIFT_BLANK = { name: '', code: '', start_time: '10:00', end_time: '21:00', break_minutes: 60, grace_minutes: 10, full_day_hours: 9,
  half_day_hours: 4.5, ot_after_minutes: 30, color: '#2F8F68', active: true }
const masterDefs = (o) => ({
  departments: { noun: 'department', blank: { name: '', code: '', head_id: '', active: true },
    fields: [['name', 'Name', 'text', true], ['code', 'Code'], ['head_id', 'Head', 'select', false, o.employees.map((e) => ({ value: e.id, label: e.name }))], ['active', 'In use', 'check']] },
  designations: { noun: 'designation', blank: { name: '', department_id: '', grade: '', level: 4, active: true },
    fields: [['name', 'Name', 'text', true], ['department_id', 'Department', 'select', false, o.departments.map((d) => ({ value: d.id, label: d.name }))],
      ['grade', 'Grade'], ['level', 'Level in the chart (1 = top)', 'number'], ['active', 'In use', 'check']] },
  shifts: { noun: 'shift', blank: SHIFT_BLANK,
    fields: [['name', 'Name', 'text', true], ['code', 'Code'], ['start_time', 'Starts', 'time', true], ['end_time', 'Ends', 'time', true],
      ['break_minutes', 'Break (min)', 'number'], ['grace_minutes', 'Grace (min)', 'number'], ['full_day_hours', 'Full day (hours)', 'number'],
      ['half_day_hours', 'Half day (hours)', 'number'], ['ot_after_minutes', 'Overtime after (min)', 'number'], ['color', 'Colour', 'color'],
      ['active', 'In use', 'check']] },
})

function MasterEditor({ kind, noun, fields, value, toast, onClose, onSaved }) {
  const [edit, setEdit] = useState(value)
  const [busy, run] = useRun(toast)
  const save = async () => {
    const body = { ...edit }
    fields.forEach(([k, , t]) => { if (t === 'number' || k.endsWith('_id')) body[k] = body[k] === '' || body[k] == null ? null : +body[k] })
    const r = await run(() => (edit.id ? api.hrSend('PUT', `masters/${kind}/${edit.id}`, body) : api.hrSend('POST', `masters/${kind}`, body)), `✓ ${noun} saved`)
    if (r) onSaved(r)
  }
  return (
    <Modal title={edit.id ? `Edit ${noun} — ${value.name}` : `New ${noun}`} onClose={onClose}
      foot={<><div className="spacer" /><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !String(edit.name || '').trim()} onClick={save}>Save</button></>}>
      <div className="fin-form">
        {fields.map(([k, l, t = 'text', req, opts]) => (
          t === 'check' ? <F key={k} label={l}><label className="fin-check"><input type="checkbox" checked={edit[k] !== false && !!edit[k]}
            onChange={(e) => setEdit({ ...edit, [k]: e.target.checked })} /> Yes</label></F>
            : t === 'date' ? <DateField key={k} label={l} required={req} value={edit[k] || ''} onChange={(v) => setEdit({ ...edit, [k]: v })} />
              : t === 'select' ? <F key={k} label={l} req={req}><Sel value={edit[k] ?? ''} onChange={(v) => setEdit({ ...edit, [k]: v })} placeholder="—" options={opts} /></F>
                : <F key={k} label={l} req={req}><input type={t} value={edit[k] ?? ''} autoFocus={k === 'name' && !edit.id}
                  onChange={(e) => setEdit({ ...edit, [k]: e.target.value })} /></F>
        ))}
      </div>
    </Modal>
  )
}

// ==========================================================================
//  HR Dashboard
// ==========================================================================
function HrDashboard({ toast, go }) {
  const [d, setD] = useState(null)
  const load = useCallback(() => api.hrGet('dashboard').then(setD).catch((e) => toast(errText(e), 'err')), [toast])
  useEffect(() => { load() }, [load])
  const open = (k) => go && go(k)
  if (!d) return <HrPage title="HR Dashboard"><Loading>Reading the people…</Loading></HrPage>
  const a = d.attendance
  const segs = [['P', a.P + a.OD], ['HD', a.HD], ['L', a.L], ['A', a.A], ['WO', a.WO + a.H], ['', a['']]]
  const total = segs.reduce((s, [, v]) => s + v, 0) || 1
  const present = a.P + a.OD + a.HD
  const pend = (d.pending_leave || 0) + (d.pending_ot || 0)
  const pr = d.payroll
  return (
    <HrPage title="HR Dashboard" sub={`${d.headcount.current} people · ${fmtDate(d.today)}`}
      actions={<>
        <button className="btn" onClick={load}><Icon name="history" size={14} /> Refresh</button>
        <button className="btn primary" onClick={() => open('hr_employees')}><Icon name="userplus" size={14} /> Employees</button></>}>
      <div className="fin-stats hr-stats">
        <Stat label="Headcount" value={d.headcount.current} accent="count" />
        <Stat label="Present today" value={`${present} / ${total - a.WO - a.H}`} accent="stock" />
        <Stat label="On leave" value={a.L} accent="move" />
        <Stat label="Absent" value={a.A} accent={a.A ? 'warn' : undefined} />
        <Stat label="Not marked" value={a['']} />
        <Stat label="Awaiting approval" value={pend} accent={pend ? 'warn' : undefined} />
        <Stat label="Monthly salary bill" value={rs0(pr.monthly_bill)} accent="money" />
        <Stat label="Attrition · 12 months" value={`${d.attrition_pct}%`} accent="back" />
      </div>
      <div className="hr-grid">
        <Card title="Today" action={<button className="btn sm" onClick={() => open('hr_attendance')}>Register</button>}>
          <div className="hr-daybar" role="img" aria-label="Today's attendance">
            {segs.filter(([, v]) => v).map(([k, v]) => <i key={k} className={'c-' + (k || 'none')} style={{ flex: v }} title={`${ST_NAME[k] || 'Not marked'}: ${v}`} />)}
          </div>
          <div className="hr-legend">
            {segs.map(([k, v]) => <span key={k}><i className={'c-' + (k || 'none')} />{k === 'WO' ? 'Off / holiday' : ST_NAME[k] || 'Not marked'} <b>{v}</b></span>)}
          </div>
          <div className="hr-two">
            <div><div className="fin-sidehead">Late in</div>
              {d.late.length ? d.late.map((x) => <div key={x.name} className="hr-li"><span>{x.name}</span><span className="muted">{x.in} · {x.minutes} min</span></div>)
                : <div className="small muted">Nobody late.</div>}</div>
            <div><div className="fin-sidehead">On leave</div>
              {d.on_leave.length ? d.on_leave.map((x) => <div key={x.name} className="hr-li"><span>{x.name}</span><span className="muted">{x.type}</span></div>)
                : <div className="small muted">Nobody on leave.</div>}</div>
          </div>
        </Card>
        <Card title="Needs attention">
          <div className="hr-alerts">
            {d.pending_leave > 0 && <button className="hr-alert" onClick={() => open('hr_leave')}><Icon name="listchecks" size={15} />
              <span>{plural(d.pending_leave, 'leave request')} to approve</span><Icon name="chevron" size={14} /></button>}
            {d.pending_ot > 0 && <button className="hr-alert" onClick={() => open('hr_overtime')}><Icon name="history" size={15} />
              <span>{plural(d.pending_ot, 'day')} of overtime to approve</span><Icon name="chevron" size={14} /></button>}
            {d.documents.map((x) => <div key={'d' + x.id} className={'hr-alert ' + (x.expired ? 'bad' : 'warn')}><Icon name="filetext" size={15} />
              <span>{x.employee}'s {x.doc_type} {x.expired ? 'expired' : 'expires'} {fmtDate(x.expires_on)}</span></div>)}
            {d.contracts.map((x) => <div key={'c' + x.id} className="hr-alert warn"><Icon name="book" size={15} />
              <span>{x.employee}'s {x.contract_type} contract ends {fmtDate(x.end_date)}</span></div>)}
            {(d.probation_over || []).map((x) => <div key={'p' + x.id} className="hr-alert warn"><Icon name="usercheck" size={15} />
              <span>{x.name}'s probation ended {fmtDate(x.date)} — confirm them on their Job tab</span></div>)}
            {d.unlinked_store > 0 && <button className="hr-alert" onClick={() => open('hr_employees')}><Icon name="store" size={15} />
              <span>{plural(d.unlinked_store, 'employee')} not linked to a Store login — no till attendance or incentive</span><Icon name="chevron" size={14} /></button>}
            {!pend && !d.documents.length && !d.contracts.length && !d.unlinked_store && !(d.probation_over || []).length && <div className="small muted">Nothing waiting.</div>}
          </div>
        </Card>
        <Card title="Coming up · 30 days">
          {d.events.length === 0 ? <div className="small muted">No birthdays, anniversaries or probation ends.</div> : d.events.map((x, i) => (
            <div key={i} className="hr-event">
              <span className="hr-date"><b>{x.date.slice(8, 10)}</b>{new Date(x.date).toLocaleString('en-IN', { month: 'short' })}</span>
              <span><b>{x.name}</b><span className="small muted">{x.kind}{x.years ? ` · ${x.years} yr` : ''}</span></span>
            </div>
          ))}
        </Card>
        <Card title="Payroll · last six months" action={<button className="btn sm" onClick={() => open('hr_payroll')}>Payroll</button>}>
          {pr.trend.length === 0 ? <div className="small muted">No payroll worked out yet.</div> : (
            <div className="hr-cols">
              {pr.trend.map((t) => {
                const max = Math.max(...pr.trend.map((x) => x.cost || x.gross || 1))
                return (
                  <div key={t.month} className="hr-col" title={`${monthName(t.month)}: cost ${rs0(t.cost)}, net ${rs0(t.net)}`}>
                    <div className="hr-coltrack"><i style={{ height: `${(t.cost || t.gross) / max * 100}%` }} />
                      <i className="net" style={{ height: `${t.net / max * 100}%` }} /></div>
                    <b>{rs0(t.cost)}</b><span>{t.label}</span><Tone s={t.status} />
                  </div>
                )
              })}
            </div>
          )}
          <div className="hr-legend"><span><i className="c-cost" />Cost to company</span><span><i className="c-net" />Net paid</span></div>
        </Card>
        <Card title="By department"><Bars rows={d.by_department} /></Card>
        <Card title="By location"><Bars rows={d.by_location} tone="alt" /></Card>
        <Card title={`Joiners & exits · ${d.month_label}`}>
          <div className="hr-two">
            <div><div className="fin-sidehead">Joined · {d.joiners.length}</div>
              {d.joiners.map((x) => <div key={x.id} className="hr-li"><span>{x.name}</span><span className="muted">{x.designation}</span></div>)}</div>
            <div><div className="fin-sidehead">Left · {d.exits.length}</div>
              {d.exits.map((x) => <div key={x.id} className="hr-li"><span>{x.name}</span><span className="muted">{x.status}</span></div>)}</div>
          </div>
        </Card>
        <Card title="Employment type"><Bars rows={d.by_type} tone="alt2" /></Card>
        <Card title="Advances outstanding" action={<button className="btn sm" onClick={() => open('hr_advances')}>Advances</button>}>
          <div className="fin-bigfig"><span>{plural(d.advances.count, 'open advance')}</span><b>{rs0(d.advances.balance)}</b></div>
          <div className="small muted fin-note">Recovered from salary each month, as each advance's instalment says.</div>
        </Card>
      </div>
    </HrPage>
  )
}

// ==========================================================================
//  Employees
// ==========================================================================
const BLANK_EMP = { name: '', code: '', father_name: '', gender: '', dob: '', marital_status: '', anniversary: '',
  blood_group: '', phone: '', alt_phone: '', email: '', address: '', permanent_address: '', emergency_name: '',
  emergency_phone: '', aadhaar: '', pan: '', uan: '', pf_no: '', esi_no: '', bank_name: '', bank_account: '', ifsc: '',
  department_id: '', designation_id: '', grade: '', location: '', reporting_to_id: '', employment_type: 'Permanent',
  joined_on: '', confirmed_on: '', probation_months: 6, shift_id: '', weekly_off: '', pf_applicable: true,
  esi_applicable: true, pt_applicable: true, tax_regime: 'new', tds_monthly: '', payment_mode: 'Bank',
  store_username: '', salary: {}, effective_on: '' }

function Employees({ toast }) {
  const [o, reloadOpts] = useHrOptions()
  const [f, setF] = useState({ q: '', department_id: '', designation_id: '', location: '', status: 'current', employment_type: '' })
  const [rows, setRows] = useState(null)
  const [open, setOpen] = useState(null)        // employee id, or 'new'
  const [importing, setImporting] = useState(false)
  const set = binder(setF)
  const load = useCallback(() => {
    api.hrGet('employees', f).then(setRows).catch((e) => { toast(errText(e), 'err'); setRows([]) })
  }, [f, toast])
  useEffect(() => { const t = setTimeout(load, 200); return () => clearTimeout(t) }, [load])
  const cols = [['code', 'Code'], ['name', 'Name'], ['designation', 'Designation'], ['department', 'Department'],
    ['location', 'Location'], ['employment_type', 'Type'], ['joined_on', 'Joined'], ['phone', 'Mobile'],
    ['store_username', 'Store login'], ['gross', 'Gross'], ['status', 'Status']]
  const total = rows ? rows.reduce((s, r) => s + (r.gross || 0), 0) : 0
  return (
    <HrPage title="Employees" sub={rows ? `${people(rows.length)} · monthly gross ${rs0(total)}` : ''}
      actions={<>
        <button className="btn primary" onClick={() => setOpen('new')}><Icon name="userplus" size={14} /> Add employee</button>
        {o.store && <button className="btn" onClick={() => setImporting(true)}><Icon name="store" size={14} /> From Store staff</button>}
        <button className="btn" disabled={!rows?.length} onClick={() => downloadCsv('employees', cols, rows)}>
          <Icon name="spreadsheet" size={14} /> Export</button></>}
      side={<>
        <F label="Search"><input value={f.q} onChange={set('q')} placeholder="Name, code, mobile or login" /></F>
        <F label="Status"><Sel value={f.status} onChange={set('status')} allowBlank={false}
          options={[{ value: 'current', label: 'Working (incl. notice)' }, { value: 'active', label: 'Active' },
            { value: 'notice', label: 'Serving notice' }, { value: 'exited', label: 'Left' }, { value: '', label: 'Everyone' }]} /></F>
        <F label="Department"><Sel value={f.department_id} onChange={set('department_id')} placeholder="All departments"
          options={o.departments.map((d) => ({ value: d.id, label: d.name }))} /></F>
        <F label="Designation"><Sel value={f.designation_id} onChange={set('designation_id')} placeholder="All designations"
          options={o.designations.map((d) => ({ value: d.id, label: d.name }))} /></F>
        <F label="Location"><Sel value={f.location} onChange={set('location')} placeholder="Everywhere" options={o.locations} /></F>
        <F label="Employment type"><Sel value={f.employment_type} onChange={set('employment_type')} placeholder="Any type" options={o.employment_types} /></F>
        <div className="small muted fin-note">Double-click a row to open the record: contracts, documents, role history,
          leave and payslips are all on it.</div>
      </>}>
      {rows === null ? <Loading>Reading the employees…</Loading> : rows.length === 0 ? (
        <Empty>Nobody matches. <button className="btn" onClick={() => setOpen('new')}>Add an employee</button></Empty>
      ) : (
        <div className="tablewrap fin-scroll"><table className="items fin-table">
          <thead><tr><th>Employee</th><th>Department</th><th>Location</th><th>Type</th><th>Joined</th><th>Mobile</th>
            <th>Store login</th><th className="r">Gross / month</th><th>Status</th></tr></thead>
          <tbody>{rows.map((e) => (
            <tr key={e.id} className="clickable" onDoubleClick={() => setOpen(e.id)}>
              <td><div className="hr-who"><Avatar id={e.id} name={e.name} photo={e.photo} />
                <div><b>{e.name}</b><div className="small muted">{e.code} · {e.designation || 'No designation'}</div></div></div></td>
              <td>{e.department || <span className="muted">—</span>}</td>
              <td>{e.location || <span className="muted">—</span>}</td>
              <td>{e.employment_type}</td>
              <td>{fmtDate(e.joined_on)}</td>
              <td className="mono">{e.phone || <span className="muted">—</span>}</td>
              <td className="mono">{e.store_username || <span className="muted">—</span>}</td>
              <td className="r b">{rs0(e.gross)}</td>
              <td><Tone s={e.status}>{e.status === 'current' ? 'active' : e.status}</Tone>
                <button className="btn sm" style={{ marginLeft: 6 }} onClick={() => setOpen(e.id)}>Open</button></td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
      {open && <EmployeeModal id={open === 'new' ? null : open} toast={toast} onClose={() => setOpen(null)}
        onSaved={() => { load(); reloadOpts() }} />}
      {importing && <StoreImport toast={toast} onClose={() => setImporting(false)} onDone={() => { setImporting(false); load(); reloadOpts() }} />}
    </HrPage>
  )
}

function StoreImport({ toast, onClose, onDone }) {
  const [st, setSt] = useState(null)
  const [pick, setPick] = useState([])
  const [busy, run] = useRun(toast)
  useEffect(() => { api.hrGet('store-staff').then(setSt).catch((e) => toast(errText(e), 'err')) }, [toast])
  const free = (st?.rows || []).filter((r) => !r.employee_id)
  const go = async () => {
    const r = await run(() => api.hrSend('POST', 'store-staff/import', { usernames: pick }), (x) => `✓ ${plural(x.made.length, 'employee')} added`)
    if (r) onDone()
  }
  return (
    <Modal title="Employees from the Store's staff" onClose={onClose}
      foot={<><span className="small muted">Each is linked to their Store login: their check-ins become attendance and their bills their incentive.</span>
        <div className="spacer" /><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !pick.length} onClick={go}>{busy ? 'Adding…' : `Add ${pick.length || ''}`}</button></>}>
      {!st ? <Loading>Reading the Store…</Loading> : !st.available ? <Empty>The Store's database cannot be read from here.</Empty> : (
        <>
          <CheckList title={`Store logins not yet employees (${free.length})`} height={320}
            items={free.map((r) => ({ value: r.username, label: `${r.name} · ${r.username} · ${r.role}${r.salary ? ' · ' + rs0(r.salary) : ''}` }))}
            value={pick} onChange={setPick} />
          <div className="small muted fin-note" style={{ marginTop: 10 }}>The Store's salary goes in as Basic — split it into HRA and
            allowances on the employee afterwards. {st.rows.length - free.length} login(s) are already linked.</div>
        </>
      )}
    </Modal>
  )
}

function EmployeeModal({ id, toast, onClose, onSaved }) {
  const [o, reloadOpts] = useHrOptions()
  const [eid, setEid] = useState(id)
  const [e, setE] = useState(null)
  const [f, setF] = useState(BLANK_EMP)
  const [tab, setTab] = useState('personal')
  const [busy, run] = useRun(toast)
  const [store, setStore] = useState(null)
  const set = binder(setF)
  const fill = (x) => {
    setE(x)
    const v = { ...BLANK_EMP }
    Object.keys(BLANK_EMP).forEach((k) => { if (x[k] != null) v[k] = x[k] })
    v.salary = { ...(x.salary || {}) }
    v.effective_on = ''
    setF(v)
  }
  const reload = useCallback(() => (eid ? api.hrGet(`employees/${eid}`).then(fill).catch((er) => toast(errText(er), 'err')) : null), [eid, toast])
  useEffect(() => { if (eid) reload(); else setF({ ...BLANK_EMP, joined_on: todayIso() }) }, [eid]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === 'job' && store === null && o.store) api.hrGet('store-staff').then(setStore).catch(() => setStore({ rows: [] })) }, [tab, o.store, store])
  const sal = (k) => (v) => setF((p) => ({ ...p, salary: { ...p.salary, [k]: v.target.value } }))
  const gross = o.components.reduce((s, [k]) => s + (+f.salary[k] || 0), 0)
  const pfBase = Math.min((+f.salary.basic || 0) + (+f.salary.da || 0), 15000)
  const erPf = f.pf_applicable ? Math.round(pfBase * 0.12) : 0
  const erEsi = f.esi_applicable && gross <= 21000 ? Math.ceil(gross * 0.0325) : 0
  const save = async () => {
    const body = { ...f }
    ;['department_id', 'designation_id', 'reporting_to_id', 'shift_id'].forEach((k) => { body[k] = body[k] ? +body[k] : null })
    body.probation_months = body.probation_months === '' ? null : +body.probation_months
    body.tds_monthly = body.tds_monthly === '' ? null : +body.tds_monthly
    body.salary = Object.fromEntries(o.components.map(([k]) => [k, +f.salary[k] || 0]))
    const r = await run(() => (eid ? api.hrSend('PUT', `employees/${eid}`, body) : api.hrSend('POST', 'employees', body)),
      (x) => `✓ ${x.name} (${x.code}) saved`)
    if (!r) return
    // the papers the form was filled from go into their file, now there is one
    const filed = []
    for (const x of reads.filter((y) => y.attach && y.status !== 'reading')) {
      const fd = new FormData()
      fd.append('doc_type', x.doc_type || 'Other')
      fd.append('number', x.number || '')
      fd.append('issued_on', x.result?.issued_on || '')
      fd.append('expires_on', x.result?.expires_on || '')
      fd.append('title', x.result?.title || '')
      fd.append('file', x.file)
      try { await api.hrUpload(`employees/${r.id}/documents`, fd); filed.push(x.id) } catch (er) { toast(`${x.file.name}: ${errText(er)}`, 'err') }
    }
    if (filed.length) {
      setReads((p) => p.filter((y) => !filed.includes(y.id)))
      toast(`✓ ${filed.length} document${filed.length === 1 ? '' : 's'} filed under Documents`)
    }
    fill(filed.length ? await api.hrGet(`employees/${r.id}`) : r)
    if (!eid) setEid(r.id)
    onSaved()
    reloadOpts()
  }
  const [reads, setReads] = useState([])          // papers dropped on the form, read and waiting to be filed
  const applyRead = async (rows) => {
    const patch = {}
    let dep = null
    let desig = null
    rows.forEach((x) => { if (x.k === 'department') dep = x; else if (x.k === 'designation') desig = x; else patch[x.k] = x.v })
    let depId = f.department_id
    if (dep) depId = String(dep.read.result.department_id || (await addMaster('departments', {})(dep.v)) || depId || '')
    let desigId = f.designation_id
    if (desig) {
      const known = desig.read.result.designation_id
      if (known) {
        desigId = String(known)
        if (!dep && desig.read.result.department_id) depId = String(desig.read.result.department_id)
      } else desigId = String((await addMaster('designations', { department_id: depId ? +depId : null })(desig.v)) || desigId || '')
    }
    setF((p) => ({ ...p, ...patch, department_id: depId, designation_id: desigId }))
  }
  const managers = o.employees.filter((x) => x.id !== eid).map((x) => ({ value: x.id, label: x.name, sub: x.designation || x.code }))
  // Lists in use, plus whatever this person already has even if since switched off.
  const live = (rows, cur) => rows.filter((r) => r.active !== false || +r.id === +cur)
  const deps = live(o.departments, f.department_id).map((d) => ({ value: d.id, label: d.name, item: d }))
  // Every designation, this department's first, each saying which department it
  // belongs to — picking one fills the department in.
  const desigs = live(o.designations, f.designation_id)
    .map((d) => ({ value: d.id, label: d.name, sub: d.department || '', item: d }))
    .sort((a, b) => (+(+b.item.department_id === +f.department_id) - +(+a.item.department_id === +f.department_id)) || a.label.localeCompare(b.label))
  const shifts = live(o.shifts, f.shift_id).map((s) => ({ value: s.id, label: s.name, sub: `${s.start_time}–${s.end_time}`, item: s }))
  const [master, setMaster] = useState(null)        // { kind, value, field } — a list entry being added or edited
  const defs = masterDefs(o)
  const editMaster = (kind, field) => (opt) => setMaster({ kind, field, value: { ...defs[kind].blank, ...opt.item } })
  const addMaster = (kind, body) => async (name) => {
    const r = await run(() => api.hrSend('POST', `masters/${kind}`, { ...body, name }), `✓ ${defs[kind].noun[0].toUpperCase() + defs[kind].noun.slice(1)} “${name}” added`)
    if (!r) return null
    await reloadOpts()
    return r.id
  }
  const pickDep = (v) => setF((p) => {
    const d = o.designations.find((x) => +x.id === +p.designation_id)
    const fits = !d || !d.department_id || !v || +d.department_id === +v
    return { ...p, department_id: v, designation_id: fits ? p.designation_id : '' }
  })
  const pickDesig = (v) => setF((p) => {
    const d = o.designations.find((x) => +x.id === +v)
    return { ...p, designation_id: v, department_id: d?.department_id ? String(d.department_id) : p.department_id, grade: p.grade || d?.grade || '' }
  })
  const formTabs = ['personal', 'job', 'pay']
  const tabs = [['personal', 'Personal'], ['job', 'Job'], ['pay', 'Pay & statutory'],
    eid && ['contracts', 'Contracts', e?.contracts?.length], eid && ['documents', 'Documents', e?.documents?.length],
    eid && ['history', 'Role history'], eid && ['leave', 'Leave'], eid && ['payslips', 'Payslips'],
    eid && ['exit', e?.status === 'active' ? 'Exit' : 'Exit details']]
  return (<>
    <Modal wide title={eid ? `${e?.name || '…'} · ${e?.code || ''}` : 'New employee'} onClose={onClose}
      foot={formTabs.includes(tab) ? <>
        <div className="spacer" />
        <button className="btn" onClick={onClose}>Close</button>
        <button className="btn primary" disabled={busy || !f.name || !f.joined_on} onClick={save}>{busy ? 'Saving…' : eid ? 'Save changes' : 'Add employee'}</button>
      </> : <><div className="spacer" /><button className="btn" onClick={onClose}>Close</button></>}>
      {eid && e && (
        <EmpBanner e={e} toast={toast} onChange={(x) => { fill(x); onSaved() }} />
      )}
      {formTabs.includes(tab) && <DocReader f={f} o={o} reads={reads} setReads={setReads} onApply={applyRead} toast={toast} />}
      <Tabs tabs={tabs} value={tab} onChange={setTab} />
      {tab === 'personal' && (
        <div className="fin-form">
          <h5>Identity</h5>
          <F label="Full name" req><input autoFocus value={f.name} onChange={set('name')} /></F>
          <F label="Father's / spouse's name"><input value={f.father_name || ''} onChange={set('father_name')} /></F>
          <F label="Gender"><Sel value={f.gender || ''} onChange={set('gender')} placeholder="—" options={o.genders} /></F>
          <DateField past label="Date of birth" value={f.dob || ''} onChange={set('dob')} />
          <F label="Marital status"><Sel value={f.marital_status || ''} onChange={set('marital_status')} placeholder="—" options={['Single', 'Married', 'Widowed', 'Divorced']} /></F>
          {f.marital_status === 'Married' && <DateField past label="Wedding anniversary" value={f.anniversary || ''} onChange={set('anniversary')} />}
          <F label="Blood group"><Sel value={f.blood_group || ''} onChange={set('blood_group')} placeholder="—" options={['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-']} /></F>
          <h5>Contact</h5>
          <F label="Mobile"><input value={f.phone || ''} onChange={set('phone')} inputMode="numeric" maxLength={13} /></F>
          <F label="Alternate mobile"><input value={f.alt_phone || ''} onChange={set('alt_phone')} /></F>
          <F label="Email"><input type="email" value={f.email || ''} onChange={set('email')} /></F>
          <F label="Emergency contact"><input value={f.emergency_name || ''} onChange={set('emergency_name')} placeholder="Name and relation" /></F>
          <F label="Emergency mobile"><input value={f.emergency_phone || ''} onChange={set('emergency_phone')} /></F>
          <F label="Current address" style={{ gridColumn: '1 / -1' }}><textarea rows={2} value={f.address || ''} onChange={set('address')} /></F>
          <F label="Permanent address" style={{ gridColumn: '1 / -1' }}><textarea rows={2} value={f.permanent_address || ''} onChange={set('permanent_address')}
            placeholder="Leave blank if the same" /></F>
        </div>
      )}
      {tab === 'job' && (
        <div className="fin-form">
          <h5>Position</h5>
          <F label="Employee code" hint={eid ? '' : 'Left blank, the next number is given'}><input value={f.code || ''} onChange={set('code')} placeholder="Auto" /></F>
          <F label="Department"><Combo value={f.department_id} onChange={pickDep} options={deps} noun="department"
            onCreate={addMaster('departments', {})} onEdit={editMaster('departments', 'department_id')} /></F>
          <F label="Designation" hint={f.designation_id ? '' : 'Picking one fills in its department'}><Combo value={f.designation_id} onChange={pickDesig}
            options={desigs} noun="designation" onCreate={addMaster('designations', { department_id: f.department_id ? +f.department_id : null, grade: f.grade || null })}
            onEdit={editMaster('designations', 'designation_id')} /></F>
          <F label="Grade"><input value={f.grade || ''} onChange={set('grade')} /></F>
          <F label="Location"><Combo value={f.location} onChange={set('location')} noun="location"
            options={f.location && !o.locations.includes(f.location) ? [...o.locations, f.location] : o.locations} onCreate={async (x) => x} /></F>
          <F label="Reports to"><Combo value={f.reporting_to_id} onChange={set('reporting_to_id')} options={managers} blankLabel="Nobody"
            emptyNote="No other employees yet — add or import them first" /></F>
          <h5>Employment</h5>
          <F label="Type"><Sel value={f.employment_type} onChange={set('employment_type')} allowBlank={false} options={o.employment_types} /></F>
          <DateField label="Joined on" required value={f.joined_on || ''} onChange={set('joined_on')} />
          <F label="Probation (months)"><input type="number" min={0} value={f.probation_months ?? ''} onChange={set('probation_months')} /></F>
          <DateField label="Confirmed on" value={f.confirmed_on || ''} onChange={set('confirmed_on')} />
          <h5>Time</h5>
          <F label="Shift"><Combo value={f.shift_id} onChange={set('shift_id')} options={shifts} noun="shift"
            onCreate={async (name) => { setMaster({ kind: 'shifts', field: 'shift_id', value: { ...SHIFT_BLANK, name } }); return null }}
            onEdit={editMaster('shifts', 'shift_id')} /></F>
          <F label="Weekly off"><Sel value={f.weekly_off || ''} onChange={set('weekly_off')} placeholder="None (rostered)" options={o.weekdays} /></F>
          <F label="Store login" hint="Their check-ins at the till become attendance; their bills, incentive">
            {store?.rows?.length ? (
              <Combo value={f.store_username} onChange={set('store_username')} blankLabel="Not linked"
                options={store.rows.filter((r) => !r.employee_id || r.employee_id === eid).map((r) => ({ value: r.username, label: r.name, sub: r.username }))} />
            ) : <input value={f.store_username || ''} onChange={set('store_username')} placeholder="Their username at the till" />}</F>
          {eid && <DateField label="Changes take effect" value={f.effective_on || ''} onChange={set('effective_on')}
            title="The date a transfer, promotion or raise is written on their role history (today if blank)" />}
        </div>
      )}
      {tab === 'pay' && (
        <div className="fin-form">
          <h5>Monthly salary</h5>
          {o.components.map(([k, l]) => (
            <F key={k} label={l}><input type="number" min={0} className="r" value={f.salary[k] ?? ''} onChange={sal(k)} placeholder="0" /></F>
          ))}
          <div className="hr-ctc" style={{ gridColumn: '1 / -1' }}>
            <span>Gross <b>{rs0(gross)}</b></span><span>Employer PF <b>{rs0(erPf)}</b></span>
            <span>Employer ESI <b>{rs0(erEsi)}</b></span><span>Cost / month <b>{rs0(gross + erPf + erEsi)}</b></span>
            <span>CTC / year <b>{rs0((gross + erPf + erEsi) * 12)}</b></span>
          </div>
          <h5>Statutory</h5>
          <F label="PF"><label className="fin-check"><input type="checkbox" checked={!!f.pf_applicable} onChange={set('pf_applicable')} /> Deduct PF</label></F>
          <F label="ESI"><label className="fin-check"><input type="checkbox" checked={!!f.esi_applicable} onChange={set('esi_applicable')} />
            Deduct ESI</label>{gross > 21000 && <div className="small muted fin-hint">Over ₹21,000 — not covered</div>}</F>
          <F label="Professional Tax"><label className="fin-check"><input type="checkbox" checked={!!f.pt_applicable} onChange={set('pt_applicable')} /> Deduct PT</label></F>
          <F label="Tax regime"><Sel value={f.tax_regime} onChange={set('tax_regime')} allowBlank={false}
            options={[{ value: 'new', label: 'New regime' }, { value: 'old', label: 'Old regime' }]} /></F>
          <F label="Fixed TDS a month" hint="Blank: estimated from the regime's slabs"><input type="number" min={0} value={f.tds_monthly ?? ''} onChange={set('tds_monthly')} /></F>
          <F label="UAN"><input value={f.uan || ''} onChange={set('uan')} maxLength={12} /></F>
          <F label="PF number"><input value={f.pf_no || ''} onChange={set('pf_no')} /></F>
          <F label="ESI IP number"><input value={f.esi_no || ''} onChange={set('esi_no')} maxLength={10} /></F>
          <F label="PAN"><input value={f.pan || ''} onChange={set('pan')} maxLength={10} style={{ textTransform: 'uppercase' }} /></F>
          <F label="Aadhaar"><input value={f.aadhaar || ''} onChange={set('aadhaar')} maxLength={14} /></F>
          <h5>Salary paid by</h5>
          <F label="Mode"><Sel value={f.payment_mode} onChange={set('payment_mode')} allowBlank={false} options={o.pay_modes} /></F>
          <F label="Bank"><input value={f.bank_name || ''} onChange={set('bank_name')} /></F>
          <F label="Account number"><input value={f.bank_account || ''} onChange={set('bank_account')} /></F>
          <F label="IFSC"><input value={f.ifsc || ''} onChange={set('ifsc')} maxLength={11} style={{ textTransform: 'uppercase' }} /></F>
        </div>
      )}
      {tab === 'contracts' && e && <EmpContracts e={e} toast={toast} onChange={reload} />}
      {tab === 'documents' && e && <EmpDocuments e={e} toast={toast} onChange={reload} />}
      {tab === 'history' && e && <EmpHistory e={e} toast={toast} onChange={reload} />}
      {tab === 'leave' && e && <EmpLeave e={e} />}
      {tab === 'payslips' && e && <EmpSlips e={e} toast={toast} />}
      {tab === 'exit' && e && <EmpExit e={e} toast={toast} onChange={() => { reload(); onSaved(); reloadOpts() }} />}
    </Modal>
    {master && <MasterEditor kind={master.kind} noun={defs[master.kind].noun} fields={defs[master.kind].fields} value={master.value}
      toast={toast} onClose={() => setMaster(null)}
      onSaved={async (r) => {
        const { field, kind } = master
        setMaster(null)
        await reloadOpts()
        if (kind === 'designations' && r.department_id && +f.designation_id === +r.id) setF((p) => ({ ...p, department_id: String(r.department_id) }))
        if (!master.value.id) setF((p) => ({ ...p, [field]: String(r.id) }))
      }} />}
  </>)
}

function EmpContracts({ e, toast, onChange }) {
  const [o] = useHrOptions()
  const blank = { contract_type: 'Appointment', start_date: todayIso(), end_date: '', ctc_annual: '', notice_days: 30, terms: '', status: 'active' }
  const [f, setF] = useState(blank)
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const add = async () => {
    const r = await run(() => api.hrSend('POST', `employees/${e.id}/contracts`, { ...f, ctc_annual: num(f.ctc_annual) }), '✓ Contract added')
    if (r) { setF(blank); onChange() }
  }
  const del = async (c) => {
    if (!window.confirm('Delete this contract?')) return
    await run(() => api.hrSend('DELETE', `employees/${e.id}/contracts/${c.id}`), 'Contract deleted')
    onChange()
  }
  return (
    <>
      {e.contracts.length === 0 ? <Empty>No contracts recorded.</Empty> : (
        <div className="tablewrap"><table className="items fin-table">
          <thead><tr><th>Contract</th><th>From</th><th>To</th><th className="r">CTC / year</th><th className="r">Notice</th><th>Status</th><th>Terms</th><th /></tr></thead>
          <tbody>{e.contracts.map((c) => (
            <tr key={c.id}><td><b>{c.contract_type}</b></td><td>{fmtDate(c.start_date)}</td><td>{fmtDate(c.end_date, 'open-ended')}</td>
              <td className="r">{c.ctc_annual ? rs0(c.ctc_annual) : '—'}</td><td className="r">{c.notice_days} d</td>
              <td><Tone s={c.status === 'active' ? 'active' : 'closed'}>{c.status}</Tone></td><td className="wrap">{c.terms}</td>
              <td className="acts"><button className="btn sm danger" onClick={() => del(c)}><Icon name="trash" size={13} /></button></td></tr>
          ))}</tbody>
        </table></div>
      )}
      <div className="fin-sidehead" style={{ marginTop: 14 }}>Add a contract</div>
      <div className="fin-form four" style={{ marginTop: 8 }}>
        <F label="Kind"><Sel value={f.contract_type} onChange={set('contract_type')} allowBlank={false} options={o.contract_types} /></F>
        <DateField label="From" required value={f.start_date} onChange={set('start_date')} />
        <DateField label="To" value={f.end_date} onChange={set('end_date')} />
        <F label="CTC a year"><input type="number" value={f.ctc_annual} onChange={set('ctc_annual')} /></F>
        <F label="Notice (days)"><input type="number" value={f.notice_days} onChange={set('notice_days')} /></F>
        <F label="Terms" style={{ gridColumn: 'span 2' }}><input value={f.terms} onChange={set('terms')} /></F>
        <F label=" "><button className="btn primary" disabled={busy || !f.start_date} onClick={add}><Icon name="plus" size={13} /> Add</button></F>
      </div>
    </>
  )
}

// The banner across the top of an employee's record: their picture, big enough
// to recognise them by, and who they are. Click the picture to see it full size.
function EmpBanner({ e, toast, onChange }) {
  const file = useRef(null)
  const [busy, run] = useRun(toast)
  const [big, setBig] = useState(false)
  const upload = async (ev) => {
    const f = ev.target.files?.[0]
    ev.target.value = ''
    if (!f) return
    const fd = new FormData()
    fd.append('file', f)
    const r = await run(() => api.hrUpload(`employees/${e.id}/photo`, fd), '✓ Photo updated')
    if (r) onChange(r)
  }
  const remove = async () => {
    if (!window.confirm(`Take ${e.name}'s photo off the record? It stays under Documents.`)) return
    const r = await run(() => api.hrSend('DELETE', `employees/${e.id}/photo`), 'Photo removed')
    if (r) onChange(r)
  }
  const chips = [
    e.phone && ['📞', e.phone], e.email && ['✉', e.email],
    e.joined_on && ['🗓', `Joined ${fmtDate(e.joined_on)} · ${e.service}`],
    ['👤', e.manager ? `Reports to ${e.manager}` : 'Reports to nobody'],
    e.store_username && ['🏬', `Store login ${e.store_username}`],
    e.blood_group && ['🩸', e.blood_group],
  ].filter(Boolean)
  return (
    <div className="hr-banner">
      <div className="hr-bannercover" />
      <div className="hr-bannerbody">
        <div className="hr-photo">
          {e.photo ? (
            <button className="hr-photobtn" onClick={() => setBig(true)} title="See the photo full size">
              <img src={`/api/hr/employees/${e.id}/photo?v=${e.photo}`} alt={`Photo of ${e.name}`} />
            </button>
          ) : <span className="hr-photoempty" aria-label="No photo">{initials(e.name)}</span>}
          <button className="hr-photocam" disabled={busy} onClick={() => file.current?.click()}
            title={e.photo ? 'Change the photo' : 'Add a photo'}><Icon name="upload" size={14} /></button>
          <input ref={file} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={upload} />
        </div>
        <div className="hr-bannerinfo">
          <div className="hr-bannername"><h3>{e.name}</h3><span className="hr-code">{e.code}</span><Tone s={e.status} /></div>
          <div className="hr-bannerrole">{[e.designation, e.department, e.location].filter(Boolean).join(' · ') || 'No job details yet'}
            {e.employment_type ? <span className="muted"> · {e.employment_type}</span> : null}</div>
          <div className="hr-chips">{chips.map(([i, t]) => <span key={t}><i aria-hidden="true">{i}</i>{t}</span>)}</div>
          <div className="hr-photoacts">
            <button className="btn sm" disabled={busy} onClick={() => file.current?.click()}>{e.photo ? 'Change photo' : 'Add photo'}</button>
            {e.photo && <button className="btn sm" onClick={() => setBig(true)}><Icon name="eye" size={13} /> View</button>}
            {e.photo && <button className="btn sm" disabled={busy} onClick={remove}>Remove</button>}
          </div>
        </div>
        <span className="hr-gross"><span>Gross / month</span><b>{rs0(e.gross)}</b></span>
      </div>
      {big && (
        <div className="hr-lightbox" role="dialog" aria-label={`Photo of ${e.name}`} onClick={() => setBig(false)}>
          <img src={`/api/hr/employees/${e.id}/photo?v=${e.photo}`} alt={`Photo of ${e.name}`} />
          <div className="hr-lightcap">{e.name} · {e.code}<span>Click anywhere to close</span></div>
        </div>
      )}
    </div>
  )
}

function DocTypeSelect({ value, onChange, groups }) {
  return (
    <select value={value} onChange={(ev) => onChange(ev.target.value)}>
      {groups.map(([g, kinds]) => <optgroup key={g} label={g}>{kinds.map((k) => <option key={k} value={k}>{k}</option>)}</optgroup>)}
    </select>
  )
}

// ---------------------------------------------------------------- filling the form from papers
const READ_LABELS = { name: 'Full name', father_name: "Father's / spouse's name", gender: 'Gender', dob: 'Date of birth',
  blood_group: 'Blood group', phone: 'Mobile', email: 'Email', address: 'Current address', permanent_address: 'Permanent address',
  aadhaar: 'Aadhaar', pan: 'PAN', uan: 'UAN', bank_name: 'Bank', bank_account: 'Account number', ifsc: 'IFSC',
  department: 'Department', designation: 'Designation', joined_on: 'Joined on', location: 'Location' }
const READ_DATES = ['dob', 'joined_on']
let readSeq = 0

// What the papers say against what the form holds, one row per field read. A row
// is ticked when the form is empty there and no earlier paper already offered it;
// a row that disagrees with the form is shown but left for the person to tick.
function readRows(reads, f, o) {
  const seen = new Set()
  const rows = []
  reads.forEach((r) => {
    if (r.status !== 'done') return
    Object.keys(READ_LABELS).forEach((k) => {
      const v = r.result.fields[k]
      if (!v) return
      const now = k === 'department' ? o.departments.find((d) => +d.id === +f.department_id)?.name || ''
        : k === 'designation' ? o.designations.find((d) => +d.id === +f.designation_id)?.name || ''
          : String(f[k] ?? '')
      const same = now.trim().toLowerCase() === String(v).trim().toLowerCase()
      rows.push({ key: `${r.id}:${k}`, k, v, now, same, from: r.doc_type, read: r, tick: !same && !now && !seen.has(k) })
      seen.add(k)
    })
  })
  return rows
}

function DocReader({ f, o, reads, setReads, onApply, toast }) {
  const pick = useRef(null)
  const [ticks, setTicks] = useState({})
  const [drag, setDrag] = useState(false)
  const rows = readRows(reads, f, o)
  const ticked = (r) => (ticks[r.key] ?? r.tick) && !r.same
  const read = (files) => {
    ;[...files].forEach((file) => {
      const id = ++readSeq
      const img = /^image\//.test(file.type)
      setReads((p) => [...p, { id, file, thumb: img ? URL.createObjectURL(file) : null, status: 'reading', attach: true }])
      const fd = new FormData()
      fd.append('file', file)
      api.hrUpload('read-document', fd)
        .then((res) => setReads((p) => p.map((x) => (x.id === id ? { ...x, status: res.doc_type ? 'done' : 'unread', result: res,
          doc_type: res.doc_type || 'Other', number: res.number || '', attach: true } : x))))
        .catch((er) => setReads((p) => p.map((x) => (x.id === id ? { ...x, status: 'error', error: errText(er), doc_type: 'Other', number: '' } : x))))
    })
  }
  const drop = (ev) => { ev.preventDefault(); setDrag(false); if (ev.dataTransfer.files?.length) read(ev.dataTransfer.files) }
  const upd = (id, patch) => setReads((p) => p.map((x) => (x.id === id ? { ...x, ...patch } : x)))
  const remove = (r) => { if (r.thumb) URL.revokeObjectURL(r.thumb); setReads((p) => p.filter((x) => x.id !== r.id)) }
  const take = rows.filter(ticked)
  const apply = async () => {
    await onApply(take)
    setTicks({})
    toast(`✓ ${take.length} field${take.length === 1 ? '' : 's'} filled — check them and save`)
  }
  const reading = reads.some((r) => r.status === 'reading')
  const notes = [...new Set(reads.map((r) => r.result?.note).filter(Boolean))]
  return (
    <div className={'hr-reader' + (drag ? ' drag' : '')} onDragOver={(ev) => { ev.preventDefault(); setDrag(true) }}
      onDragLeave={() => setDrag(false)} onDrop={drop}>
      <div className="hr-readerhead">
        <span className="hr-readericon"><Icon name="sparkle" size={16} /></span>
        <div><b>Fill from documents</b>
          <div className="small muted">Aadhaar, PAN, voter ID, licence, passport, cheque or passbook, resume, offer letter — drop them here.
            What is read is shown below to check before it goes into the form, and the papers are filed under Documents when you save.</div></div>
        <button type="button" className="btn sm" onClick={() => pick.current?.click()}><Icon name="upload" size={13} /> Upload</button>
        <input ref={pick} type="file" multiple hidden accept=".pdf,.jpg,.jpeg,.png,.webp"
          onChange={(ev) => { read(ev.target.files); ev.target.value = '' }} />
      </div>
      {reads.length > 0 && (
        <div className="hr-readdocs">
          {reads.map((r) => (
            <div key={r.id} className={'hr-readdoc ' + r.status}>
              {r.thumb ? <img src={r.thumb} alt="" /> : <span className="hr-docicon"><Icon name="filetext" size={16} /></span>}
              <div className="hr-readdocbody">
                <div className="hr-readdoctop">
                  <span className="hr-readdocname" title={r.file.name}>{r.file.name}</span>
                  <span className="small muted">{r.status === 'reading' ? <><Hourglass size={13} /> Reading…</> : r.status === 'done' ? `${Object.keys(r.result.fields).length} read` : r.status === 'unread' ? 'not read' : ''}</span>
                  <button type="button" className="hr-readx" title="Leave this one out" aria-label={`Remove ${r.file.name}`} onClick={() => remove(r)}><Icon name="x" size={13} /></button>
                </div>
                {r.status === 'error' ? <div className="small hr-bad">{r.error}</div>
                  : r.status !== 'reading' && <div className="hr-readdocmeta">
                    <DocTypeSelect value={r.doc_type} onChange={(v) => upd(r.id, { doc_type: v })} groups={o.doc_groups.length ? o.doc_groups : [['Documents', o.doc_types]]} />
                    <label className="hr-fileit" title="File this paper under the employee's Documents on saving">
                      <input type="checkbox" checked={r.attach} onChange={(ev) => upd(r.id, { attach: ev.target.checked })} /> File it</label>
                  </div>}
              </div>
            </div>
          ))}
        </div>
      )}
      {notes.map((n) => <div key={n} className="small muted hr-readnote">{n}</div>)}
      {rows.length > 0 && (
        <>
          <div className="tablewrap"><table className="items fin-table hr-readtable">
            <thead><tr><th style={{ width: 30 }} /><th>Field</th><th>Read from the document</th><th>In the form now</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.key} className={r.same ? 'same' : !r.now ? '' : 'differs'}>
                <td>{r.same ? <span className="hr-good" aria-label="Same as the form">✓</span>
                  : <input type="checkbox" checked={!!ticked(r)} aria-label={`Use ${READ_LABELS[r.k]}`}
                    onChange={(ev) => setTicks({ ...ticks, [r.key]: ev.target.checked })} />}</td>
                <td><b>{READ_LABELS[r.k]}</b><div className="small muted">{r.from}</div></td>
                <td className={r.k === 'aadhaar' || r.k === 'pan' || r.k === 'ifsc' || r.k === 'bank_account' ? 'mono' : ''}>
                  {READ_DATES.includes(r.k) ? fmtDate(r.v) : r.v}
                  {r.k === 'department' && !r.read.result.department_id && <Badge tone="pending">new</Badge>}
                  {r.k === 'designation' && !r.read.result.designation_id && <Badge tone="pending">new</Badge>}</td>
                <td className="muted">{r.same ? 'Same' : r.now ? (READ_DATES.includes(r.k) ? fmtDate(r.now) : r.now) : '—'}</td>
              </tr>
            ))}</tbody>
          </table></div>
          <div className="hr-readfoot">
            <span className="small muted">Ticked fields replace what the form holds. New departments and designations are added to the lists.</span>
            <button type="button" className="btn primary" disabled={!take.length || reading} onClick={apply}>
              <Icon name="clipcheck" size={14} /> Fill {take.length || ''} field{take.length === 1 ? '' : 's'}</button>
          </div>
        </>
      )}
    </div>
  )
}

function EmpDocuments({ e, toast, onChange }) {
  const [o] = useHrOptions()
  const [f, setF] = useState({ doc_type: 'Offer letter', number: '', issued_on: '', expires_on: '', title: '' })
  const [open, setOpen] = useState(null)       // a picture shown full size
  const file = useRef(null)
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const groups = o.doc_groups?.length ? o.doc_groups : [['Documents', o.doc_types]]
  const add = async () => {
    const fd = new FormData()
    Object.entries(f).forEach(([k, v]) => fd.append(k, v || ''))
    if (file.current?.files?.[0]) fd.append('file', file.current.files[0])
    const r = await run(() => api.hrUpload(`employees/${e.id}/documents`, fd), `✓ ${f.doc_type} added`)
    if (r) { setF({ ...f, number: '', issued_on: '', expires_on: '', title: '' }); if (file.current) file.current.value = ''; onChange() }
  }
  const del = async (d) => {
    if (!window.confirm(`Delete ${d.doc_type}?`)) return
    await run(() => api.hrSend('DELETE', `employees/${e.id}/documents/${d.id}`), 'Document deleted')
    onChange()
  }
  const url = (d) => `/api/hr/employees/${e.id}/documents/${d.id}/file`
  const inGroup = (g) => e.documents.filter((d) => (d.group || 'Other') === g)
  return (
    <>
      <div className="hr-docadd">
        <div className="fin-sidehead">Add to the file</div>
        <div className="fin-form four" style={{ marginTop: 8, marginBottom: 0 }}>
          <F label="Document"><DocTypeSelect value={f.doc_type} onChange={set('doc_type')} groups={groups} /></F>
          <F label="Number"><input value={f.number} onChange={set('number')} /></F>
          <DateField label="Issued" value={f.issued_on} onChange={set('issued_on')} />
          <DateField label="Expires" value={f.expires_on} onChange={set('expires_on')} />
          <F label="Note"><input value={f.title} onChange={set('title')} placeholder={f.doc_type === 'Certificate' ? 'e.g. Tailoring course' : ''} /></F>
          <F label="File (PDF, picture or Word · up to 10 MB)" style={{ gridColumn: 'span 2' }}>
            <input ref={file} type="file" accept={f.doc_type === 'Photo' ? 'image/jpeg,image/png,image/webp' : '.pdf,.jpg,.jpeg,.png,.webp,.doc,.docx'} /></F>
          <F label=" "><button className="btn primary" disabled={busy} onClick={add}><Icon name="upload" size={13} /> Upload</button></F>
        </div>
      </div>
      <div className="hr-folders">
        {groups.map(([g]) => {
          const docs = inGroup(g)
          return (
            <section key={g} className={'hr-folder' + (docs.length ? '' : ' none')}>
              <div className="hr-folderhead"><Icon name="book" size={14} /><b>{g}</b><span className="hr-count">{docs.length}</span></div>
              {docs.length === 0 ? <div className="small muted hr-foldernone">Nothing filed yet</div> : docs.map((d) => (
                <div key={d.id} className="hr-doc">
                  {d.is_image ? <button className="hr-docthumb" onClick={() => setOpen(d)} title="See it full size">
                    <img src={url(d)} alt={d.doc_type} loading="lazy" /></button>
                    : <span className="hr-docicon"><Icon name="filetext" size={18} /></span>}
                  <div className="hr-docbody">
                    <b>{d.doc_type}</b>{d.title && <span className="muted"> · {d.title}</span>}
                    <div className="small muted">{[d.number, d.issued_on && `issued ${fmtDate(d.issued_on)}`, d.expires_on && `expires ${fmtDate(d.expires_on)}`]
                      .filter(Boolean).join(' · ') || (d.filename || 'No details')}
                      {d.expired && <Badge tone="cancelled">expired</Badge>}{d.expiring && <Badge tone="pending">expiring</Badge>}</div>
                  </div>
                  <div className="hr-docacts">
                    {d.has_file && <a className="btn sm" href={url(d)} target="_blank" rel="noreferrer" title={d.filename}><Icon name="eye" size={13} /> View</a>}
                    <button className="btn sm danger" onClick={() => del(d)} title="Delete"><Icon name="trash" size={13} /></button>
                  </div>
                </div>
              ))}
            </section>
          )
        })}
        <section className="hr-folder note">
          <div className="hr-folderhead"><Icon name="receipt" size={14} /><b>Payslips</b></div>
          <div className="small muted hr-foldernone">Made from each month's payroll — see the Payslips tab. They are never out of step with the books.</div>
        </section>
      </div>
      {open && (
        <div className="hr-lightbox" role="dialog" aria-label={open.doc_type} onClick={() => setOpen(null)}>
          <img src={url(open)} alt={open.doc_type} />
          <div className="hr-lightcap">{e.name} · {open.doc_type}<span>Click anywhere to close</span></div>
        </div>
      )}
    </>
  )
}

function EmpHistory({ e, toast, onChange }) {
  const [o] = useHrOptions()
  const [f, setF] = useState({ kind: 'Note', effective_on: todayIso(), detail: '' })
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const add = async () => {
    const r = await run(() => api.hrSend('POST', `employees/${e.id}/history`, f), '✓ Added to the history')
    if (r) { setF({ ...f, detail: '' }); onChange() }
  }
  return (
    <>
      <div className="hr-timeline">
        {e.history.map((h) => (
          <div key={h.id} className={'hr-tl k-' + String(h.kind).toLowerCase().replace(/[^a-z]/g, '')}>
            <span className="hr-tldate">{fmtDate(h.effective_on)}</span>
            <div className="hr-tlbody"><b>{h.kind}</b><div>{h.detail}</div>
              <div className="small muted">{[h.designation, h.department, h.location].filter(Boolean).join(' · ')}
                {h.gross ? ` · ${rs0(h.gross)}/month` : ''}{h.created_by ? ` · by ${h.created_by}` : ''}</div></div>
          </div>
        ))}
      </div>
      <div className="small muted fin-note">Changes of designation, department, place, pay and manager are written here by the
        employee form itself. Add anything else below.</div>
      <div className="fin-form four" style={{ marginTop: 8 }}>
        <F label="Kind"><Sel value={f.kind} onChange={set('kind')} allowBlank={false} options={o.history_kinds} /></F>
        <DateField label="Effective" value={f.effective_on} onChange={set('effective_on')} />
        <F label="What happened" style={{ gridColumn: 'span 1' }}><input value={f.detail} onChange={set('detail')} /></F>
        <F label=" "><button className="btn primary" disabled={busy || !f.detail} onClick={add}><Icon name="plus" size={13} /> Add</button></F>
      </div>
    </>
  )
}

function EmpLeave({ e }) {
  const [ledger, setLedger] = useState(null)
  useEffect(() => { api.hrGet(`employees/${e.id}/leave-ledger`).then(setLedger).catch(() => setLedger([])) }, [e.id])
  return (
    <>
      <div className="hr-leavecards">{e.leave.map((b) => (
        <div key={b.leave_type_id} className="hr-leavecard" style={{ '--lc': b.color }}>
          <span>{b.code}</span><b>{b.balance}</b><small>{b.name}{b.pending ? ` · ${b.pending} pending` : ''}</small>
          <small className="muted">taken this year {b.taken_this_year}</small>
        </div>))}</div>
      <div className="fin-sidehead" style={{ marginTop: 14 }}>Every day added and taken</div>
      {!ledger ? <Loading>Reading…</Loading> : ledger.length === 0 ? <Empty>Nothing yet — run the month's accrual on Leave.</Empty> : (
        <div className="tablewrap"><table className="items fin-table">
          <thead><tr><th>Date</th><th>Leave</th><th>What</th><th className="r">Days</th><th className="r">Balance</th><th>Note</th></tr></thead>
          <tbody>{ledger.map((x) => (
            <tr key={x.id}><td>{fmtDate(x.date)}</td><td>{x.type}</td><td>{x.kind}</td>
              <td className={'r ' + (x.days < 0 ? 'neg' : 'pos')}>{x.days > 0 ? '+' : ''}{x.days}</td><td className="r b">{x.balance}</td>
              <td className="small muted">{x.remarks}</td></tr>
          ))}</tbody>
        </table></div>
      )}
    </>
  )
}

function EmpSlips({ e, toast }) {
  const [rows, setRows] = useState(null)
  useEffect(() => { api.hrGet(`employees/${e.id}/payslips`).then(setRows).catch(() => setRows([])) }, [e.id])
  return !rows ? <Loading>Reading…</Loading> : rows.length === 0 ? <Empty>No payslips yet.</Empty> : (
    <div className="tablewrap"><table className="items fin-table">
      <thead><tr><th>Month</th><th>Status</th><th className="r">Paid days</th><th className="r">Gross</th><th className="r">Deductions</th><th className="r">Net</th><th /></tr></thead>
      <tbody>{rows.map((s) => (
        <tr key={s.id}><td><b>{s.label}</b></td><td><Tone s={s.status} /></td><td className="r">{s.paid_days}</td>
          <td className="r">{rs0(s.gross)}</td><td className="r">{rs0(s.total_deductions)}</td><td className="r b">{rs0(s.net)}</td>
          <td className="acts"><button className="btn sm" onClick={() => printSlips([s.id], toast)}><Icon name="printer" size={13} /> Payslip</button></td></tr>
      ))}</tbody>
    </table></div>
  )
}

function EmpExit({ e, toast, onChange }) {
  const [o] = useHrOptions()
  const [f, setF] = useState({ resigned_on: e.resigned_on || todayIso(), last_working_day: e.last_working_day || '',
    exit_type: e.exit_type || 'Resigned', exit_reason: e.exit_reason || '', fnf_status: e.fnf_status || 'pending', fnf_amount: e.fnf_amount ?? '' })
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const save = async () => {
    const r = await run(() => api.hrSend('POST', `employees/${e.id}/exit`, { ...f, fnf_amount: num(f.fnf_amount) }), '✓ Exit recorded')
    if (r) onChange()
  }
  const rejoin = async () => {
    if (!window.confirm(`Bring ${e.name} back as active from today?`)) return
    const r = await run(() => api.hrSend('POST', `employees/${e.id}/rejoin`, {}), '✓ Rejoined')
    if (r) onChange()
  }
  return (
    <>
      {e.status !== 'active' && <div className="fin-warn" style={{ marginBottom: 12 }}>
        {e.status === 'notice' ? `Serving notice — last working day ${fmtDate(e.last_working_day)}.` : `Left on ${fmtDate(e.last_working_day)} (${e.exit_type}).`}
        {' '}Their records, payslips and history stay. <button className="btn sm" onClick={rejoin}>Rejoin</button></div>}
      <div className="fin-form">
        <DateField label="Resigned / notified on" value={f.resigned_on} onChange={set('resigned_on')} />
        <DateField label="Last working day" required value={f.last_working_day} onChange={set('last_working_day')} />
        <F label="Reason for leaving"><Sel value={f.exit_type} onChange={set('exit_type')} allowBlank={false} options={o.exit_types} /></F>
        <F label="Full & final"><Sel value={f.fnf_status} onChange={set('fnf_status')} allowBlank={false}
          options={[{ value: 'pending', label: 'Pending' }, { value: 'settled', label: 'Settled' }]} /></F>
        <F label="F&F amount"><input type="number" value={f.fnf_amount} onChange={set('fnf_amount')} /></F>
        <F label="Notes" style={{ gridColumn: '1 / -1' }}><textarea rows={2} value={f.exit_reason} onChange={set('exit_reason')} /></F>
      </div>
      <div className="small muted fin-note">A last day in the future puts them on notice; on that day they move to Left by
        themselves. Anyone reporting to them moves up to their manager.</div>
      <div style={{ marginTop: 12 }}><button className="btn primary" disabled={busy || !f.last_working_day} onClick={save}>
        {e.status === 'active' ? 'Record the exit' : 'Update exit details'}</button></div>
    </>
  )
}

// ==========================================================================
//  Organisation chart
// ==========================================================================
function OrgChart({ toast }) {
  const [o] = useHrOptions()
  const [f, setF] = useState({ department_id: '', location: '' })
  const [d, setD] = useState(null)
  const [view, setView] = useState('tree')
  const [open, setOpen] = useState(null)
  const [zoom, setZoom] = useState(1)
  const set = binder(setF)
  const load = useCallback(() => api.hrGet('org-chart', f).then(setD).catch((e) => toast(errText(e), 'err')), [f, toast])
  useEffect(() => { load() }, [load])
  const kids = useMemo(() => {
    const m = {}
    ;(d?.nodes || []).forEach((n) => { (m[n.manager_id || 0] = m[n.manager_id || 0] || []).push(n) })
    Object.values(m).forEach((l) => l.sort((a, b) => (a.level - b.level) || a.name.localeCompare(b.name)))
    return m
  }, [d])
  const Node = ({ n }) => (
    <li>
      <button className="hr-node" onClick={() => setOpen(n.id)} title="Open the record">
        <Avatar id={n.id} name={n.name} photo={n.photo} size={38} />
        <span className="hr-nodetext"><b>{n.name}</b><span>{n.designation || '—'}</span>
          <small>{[n.department, n.location].filter(Boolean).join(' · ')}</small></span>
        {n.reports > 0 && <em title={plural(n.reports, 'direct report')}>{n.reports}</em>}
      </button>
      {(kids[n.id] || []).length > 0 && <ul>{kids[n.id].map((k) => <Node key={k.id} n={k} />)}</ul>}
    </li>
  )
  return (
    <HrPage title="Organisation Chart" sub={d ? `${people(d.nodes.length)} · ${people(d.roots)} at the top` : ''}
      actions={view === 'tree' && <div className="fin-inline">
        <button className="btn icon" onClick={() => setZoom((z) => Math.max(0.5, z - 0.1))} title="Smaller">−</button>
        <button className="btn icon" onClick={() => setZoom(1)} title="Actual size">{Math.round(zoom * 100)}%</button>
        <button className="btn icon" onClick={() => setZoom((z) => Math.min(1.4, z + 0.1))} title="Larger">+</button></div>}
      side={<>
        <F label="Department"><Sel value={f.department_id} onChange={set('department_id')} placeholder="Every department"
          options={o.departments.map((x) => ({ value: x.id, label: x.name }))} /></F>
        <F label="Location"><Sel value={f.location} onChange={set('location')} placeholder="Everywhere" options={o.locations} /></F>
        <div className="small muted fin-note">The lines are “Reports to” on each employee's Job tab. Someone who reports to
          nobody sits at the top. Click a person to open their record.</div>
      </>}>
      <Tabs tabs={[['tree', 'Reporting lines'], ['departments', 'Departments']]} value={view} onChange={setView} />
      {!d ? <Empty>Drawing the chart…</Empty> : d.nodes.length === 0 ? <Empty>No employees yet.</Empty> : view === 'tree' ? (
        <div className="hr-treewrap"><div className="hr-tree" style={{ transform: `scale(${zoom})` }}>
          <ul>{(kids[0] || []).map((n) => <Node key={n.id} n={n} />)}</ul></div></div>
      ) : (
        <div className="hr-depgrid">{d.departments.map((dep) => {
          const members = d.nodes.filter((n) => n.department === dep.name)
          return (
            <div key={dep.id} className="hr-card">
              <div className="hr-cardhead"><h4>{dep.name}</h4><span className="hr-count">{dep.count}</span></div>
              <div className="hr-cardbody">
                <div className="small muted" style={{ marginBottom: 8 }}>Head: {dep.head || 'not set'}</div>
                {members.map((n) => <button key={n.id} className="hr-member" onClick={() => setOpen(n.id)}>
                  <Avatar id={n.id} name={n.name} photo={n.photo} size={26} /><span>{n.name}</span><small>{n.designation}</small></button>)}
              </div>
            </div>
          )
        })}</div>
      )}
      {open && <EmployeeModal id={open} toast={toast} onClose={() => setOpen(null)} onSaved={load} />}
    </HrPage>
  )
}

// ==========================================================================
//  Attendance
// ==========================================================================
function Attendance({ toast }) {
  const [o] = useHrOptions()
  const [f, setF] = useState({ month: thisMonth(), department_id: '', location: '' })
  const [d, setD] = useState(null)
  const [cell, setCell] = useState(null)
  const [bulk, setBulk] = useState(false)
  const [devices, setDevices] = useState(false)
  const [busy, run] = useRun(toast)
  const fileRef = useRef(null)
  const set = binder(setF)
  const load = useCallback(() => api.hrGet('attendance', f).then(setD).catch((e) => toast(errText(e), 'err')), [f, toast])
  useEffect(() => { setD(null); load() }, [load])
  const importFile = async (ev) => {
    const file = ev.target.files?.[0]
    if (!file) return
    const fd = new FormData()
    fd.append('file', file)
    const r = await run(() => api.hrUpload('attendance/import', fd),
      (x) => `✓ ${plural(x.days, 'day')} read${x.unknown.length ? ` · ${x.unknown.length} code(s) not recognised: ${x.unknown.map((u) => u.key).join(', ')}` : ''}`)
    ev.target.value = ''
    if (r) load()
  }
  const syncStore = async () => {
    const days = d?.days || []
    const r = await run(() => api.hrSend('POST', 'attendance/sync-store', { date_from: days[0]?.date, date_to: days[days.length - 1]?.date }),
      (x) => `✓ ${plural(x.days, 'day')} from the Store's check-ins${x.unknown.length ? ` · ${x.unknown.length} login(s) not linked to an employee` : ''}`)
    if (r) load()
  }
  const exportCsv = () => {
    const cols = [['code', 'Code'], ['name', 'Name'], ...d.days.map((x) => [(r) => r.days[x.date]?.s || '', String(x.d)]),
      [(r) => r.totals.present, 'Present'], [(r) => r.totals.paid_leave, 'Paid leave'], [(r) => r.totals.lop, 'LOP'],
      [(r) => r.totals.wo, 'Weekly off'], [(r) => r.totals.holiday, 'Holidays'], [(r) => r.totals.paid_days, 'Paid days']]
    downloadCsv(`attendance-${f.month}`, cols, d.rows)
  }
  return (
    <HrPage title="Attendance" sub={d ? `${d.label} · ${people(d.rows.length)}` : ''}
      actions={<>
        <button className="btn" onClick={() => setBulk(true)} disabled={!d}><Icon name="listchecks" size={14} /> Mark many</button>
        <button className="btn" onClick={() => fileRef.current?.click()} disabled={busy}><Icon name="upload" size={14} /> Import file</button>
        <input ref={fileRef} type="file" accept=".csv,text/csv" hidden onChange={importFile} />
        {o.store && <button className="btn" onClick={syncStore} disabled={busy || !d}><Icon name="store" size={14} /> From Store check-ins</button>}
        <button className="btn" onClick={() => setDevices(true)}><Icon name="cpu" size={14} /> Devices</button>
        <button className="btn" onClick={exportCsv} disabled={!d?.rows.length}><Icon name="spreadsheet" size={14} /> Export</button></>}
      side={<>
        <MonthField value={f.month} onChange={set('month')} />
        <F label="Department"><Sel value={f.department_id} onChange={set('department_id')} placeholder="Every department"
          options={o.departments.map((x) => ({ value: x.id, label: x.name }))} /></F>
        <F label="Location"><Sel value={f.location} onChange={set('location')} placeholder="Everywhere" options={o.locations} /></F>
        <div className="fin-sidehead">Key</div>
        <div className="hr-key">{Object.entries(ST_NAME).map(([k, l]) => <span key={k}><i className={'hr-st c-' + k}>{k}</i>{l}</span>)}
          <span><i className="hr-st derived">WO</i>Not marked, worked out</span>
          <span><i className="hr-st c-P late">P</i>Late in</span><span><i className="hr-st c-P ot">P</i>Overtime</span></div>
        <div className="small muted fin-note">Click a day to mark it. Times entered work out late arrival and overtime from the
          person's shift. Days nobody marked count as {'present'} on payroll unless HR Settings says otherwise.</div>
      </>}>
      {!d ? <Loading>Reading the register…</Loading> : d.rows.length === 0 ? <Empty>Nobody employed in this month.</Empty> : (
        <div className="tablewrap fin-scroll hr-attwrap"><table className="items hr-att">
          <thead><tr><th className="sticky">Employee</th>
            {d.days.map((x) => <th key={x.date} className={(x.holiday ? 'hol ' : '') + (x.today ? 'today ' : '') + (x.dow === 'Su' ? 'sun' : '')}
              title={x.holiday || ''}><b>{x.d}</b><span>{x.dow}</span></th>)}
            <th className="tot">P</th><th className="tot">Leave</th><th className="tot">LOP</th><th className="tot">Off</th><th className="tot">OT h</th><th className="tot">Paid</th></tr></thead>
          <tbody>{d.rows.map((r) => (
            <tr key={r.id}>
              <td className="sticky"><b>{r.name}</b><div className="small muted">{r.code}{r.shift ? ` · ${r.shift}` : ''}</div></td>
              {d.days.map((x) => {
                const c = r.days[x.date] || {}
                if (c.out) return <td key={x.date} className="na" />
                return (
                  <td key={x.date} className="cell">
                    <button className={'hr-st c-' + (c.s || 'none') + (c.derived ? ' derived' : '') + (c.late ? ' late' : '') + (c.ot ? ' ot' : '')}
                      title={`${fmtDate(x.date)} · ${ST_NAME[c.s] || 'Not marked'}${c.in ? ` · ${c.in}–${c.out_t || '?'}` : ''}${c.late ? ` · ${c.late} min late` : ''}${c.ot ? ` · OT ${hrs(c.ot)} h` : ''}${c.rm ? ` · ${c.rm}` : ''}`}
                      onClick={() => setCell({ r, x, c })}>{c.s || '·'}</button>
                  </td>
                )
              })}
              <td className="tot">{r.totals.present}</td><td className="tot">{r.totals.paid_leave}</td>
              <td className={'tot' + (r.totals.lop ? ' neg' : '')}>{r.totals.lop}</td><td className="tot">{r.totals.wo + r.totals.holiday}</td>
              <td className="tot">{hrs(r.totals.ot_approved)}</td><td className="tot b">{r.totals.paid_days}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
      {cell && <DayModal {...cell} toast={toast} onClose={() => setCell(null)} onSaved={() => { setCell(null); load() }} />}
      {bulk && d && <BulkMark d={d} toast={toast} onClose={() => setBulk(false)} onDone={() => { setBulk(false); load() }} />}
      {devices && <DevicesModal toast={toast} onClose={() => setDevices(false)} />}
    </HrPage>
  )
}

function DayModal({ r, x, c, toast, onClose, onSaved }) {
  const [f, setF] = useState({ status: c.derived ? '' : c.s || '', in_time: c.in || '', out_time: c.out_t || '', remarks: c.rm || '' })
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const leave = c.src === 'leave'
  const save = async () => {
    const res = await run(() => api.hrSend('PUT', `attendance/${r.id}/${x.date}`, f), '✓ Saved')
    if (res) onSaved()
  }
  const clear = async () => {
    const res = await run(() => api.hrSend('DELETE', `attendance/${r.id}/${x.date}`), 'Cleared')
    if (res) onSaved()
  }
  return (
    <Modal title={`${r.name} · ${fmtDate(x.date)} (${x.dow})`} onClose={onClose}
      foot={<>{!c.derived && !leave && <button className="btn danger" onClick={clear} disabled={busy}>Clear the day</button>}
        <div className="spacer" /><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || leave} onClick={save}>Save</button></>}>
      {leave && <div className="fin-warn" style={{ marginBottom: 12 }}>Approved leave ({c.rm}). Cancel the leave on the Leave screen to change it.</div>}
      {c.src && !leave && <div className="small muted" style={{ marginBottom: 10 }}>Recorded from: {c.src}{c.late ? ` · ${c.late} min late` : ''}{c.ot ? ` · overtime ${hrs(c.ot)} h` : ''}</div>}
      <div className="fin-form">
        <F label="Status" hint="Blank: worked out from the times"><Sel value={f.status} onChange={set('status')} placeholder="From the times"
          options={Object.entries(ST_NAME).filter(([k]) => k !== 'L').map(([k, l]) => ({ value: k, label: `${k} — ${l}` }))} /></F>
        <F label="In"><input type="time" value={f.in_time} onChange={set('in_time')} /></F>
        <F label="Out"><input type="time" value={f.out_time} onChange={set('out_time')} /></F>
        <F label="Remarks" style={{ gridColumn: '1 / -1' }}><input value={f.remarks} onChange={set('remarks')} /></F>
      </div>
    </Modal>
  )
}

function BulkMark({ d, toast, onClose, onDone }) {
  const [ids, setIds] = useState([])
  const [f, setF] = useState({ date: todayIso() >= d.days[0].date && todayIso() <= d.days[d.days.length - 1].date ? todayIso() : d.days[0].date,
    date_to: '', status: 'P', remarks: '' })
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const go = async () => {
    const dates = []
    const a = new Date(f.date)
    const b = new Date(f.date_to || f.date)
    for (let t = new Date(a); t <= b; t.setDate(t.getDate() + 1)) dates.push(t.toISOString().slice(0, 10))
    const r = await run(() => api.hrSend('POST', 'attendance/bulk', { employee_ids: ids, dates, status: f.status, remarks: f.remarks }),
      (x) => `✓ ${plural(x.marked, 'day')} marked`)
    if (r) onDone()
  }
  return (
    <Modal title="Mark many" onClose={onClose}
      foot={<><div className="spacer" /><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !ids.length} onClick={go}>Mark {ids.length || ''}</button></>}>
      <div className="fin-form">
        <DateField label="From" value={f.date} onChange={set('date')} />
        <DateField label="To (optional)" value={f.date_to} onChange={set('date_to')} />
        <F label="Status"><Sel value={f.status} onChange={set('status')} allowBlank={false}
          options={Object.entries(ST_NAME).filter(([k]) => k !== 'L').map(([k, l]) => ({ value: k, label: `${k} — ${l}` }))} /></F>
        <F label="Remarks"><input value={f.remarks} onChange={set('remarks')} /></F>
      </div>
      <CheckList title="People" height={260} items={d.rows.map((r) => ({ value: r.id, label: `${r.name} · ${r.code}` }))} value={ids} onChange={setIds} />
    </Modal>
  )
}

function DevicesModal({ toast, onClose }) {
  return (
    <Modal wide title="Attendance devices & files" onClose={onClose} foot={<><div className="spacer" /><button className="btn primary" onClick={onClose}>Close</button></>}>
      <DevicesBody toast={toast} />
    </Modal>
  )
}

function DevicesBody({ toast }) {
  const [s, setS] = useState(null)
  const [busy, run] = useRun(toast)
  useEffect(() => { api.hrGet('settings').then(setS).catch((e) => toast(errText(e), 'err')) }, [toast])
  const url = `${window.location.origin}/api/hr/device/punches`
  const renew = async () => {
    if (!window.confirm('Make a new key? Every device using the old one stops being accepted.')) return
    const r = await run(() => api.hrSend('POST', 'settings/device-key'), '✓ New key made')
    if (r) setS({ ...s, device_key: r.device_key })
  }
  const copy = (t) => navigator.clipboard?.writeText(t).then(() => toast('Copied', 'ok'))
  return (
      <div className="hr-howto">
        <section><h5>1 · A device or its software pushes punches</h5>
          <p>Biometric readers, face terminals and their attendance software can send each punch here as it happens.</p>
          <F label="Address (POST)"><div className="fin-inline"><input readOnly value={url} /><button className="btn sm" onClick={() => copy(url)}>Copy</button></div></F>
          <F label="Header X-Attendance-Key"><div className="fin-inline"><input readOnly value={s?.device_key || '…'} />
            <button className="btn sm" onClick={() => copy(s?.device_key || '')}>Copy</button>
            <button className="btn sm" disabled={busy} onClick={renew}>New key</button></div></F>
          <pre className="hr-code">{`{"punches": [
  {"id": "EMP0001", "time": "2026-11-04 09:58"},
  {"id": "priya",   "time": "2026-11-04 21:02"}
]}`}</pre>
          <p className="small muted"><b>id</b> is the employee code, their Store login, or the device's enrolment number if it is
            entered as the code. The first punch of a day is the in, the last the out.</p></section>
        <section><h5>2 · A file exported from any system</h5>
          <p>Import file on the register reads a CSV with headings. Either one row a day — <code>Emp Code, Date, In Time, Out Time, Status</code> —
            or one row a punch — <code>Emp Code, Punch Time</code>. Excel sheets: save as CSV first.</p></section>
        <section><h5>3 · The Store's own check-in</h5>
          <p>Staff sign in at the till; “From Store check-ins” turns those sign-ins into the day, for everyone whose Store login is
            on their employee record.</p></section>
      </div>
  )
}

// ==========================================================================
//  Shifts & roster
// ==========================================================================
function mondayOf(iso) {
  const d = new Date(iso)
  const day = (d.getDay() + 6) % 7
  d.setDate(d.getDate() - day)
  return d.toISOString().slice(0, 10)
}
const addDays = (iso, n) => { const d = new Date(iso); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10) }

function Shifts({ toast }) {
  const [o, reloadOpts] = useHrOptions()
  const [tab, setTab] = useState('roster')
  const [f, setF] = useState({ start: mondayOf(todayIso()), department_id: '', location: '' })
  const [d, setD] = useState(null)
  const [pick, setPick] = useState([])
  const [a, setA] = useState({ date_from: '', date_to: '', shift_id: '', mode: 'shift' })
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const setAA = binder(setA)
  const load = useCallback(() => api.hrGet('roster', f).then((x) => { setD(x); setA((p) => ({ ...p, date_from: p.date_from || x.start, date_to: p.date_to || addDays(x.start, 6) })) })
    .catch((e) => toast(errText(e), 'err')), [f, toast])
  useEffect(() => { if (tab === 'roster') load() }, [load, tab])
  const shift = Object.fromEntries(o.shifts.map((s) => [s.id, s]))
  const assign = async () => {
    const body = { employee_ids: pick, date_from: a.date_from, date_to: a.date_to || a.date_from,
      shift_id: a.mode === 'shift' ? +a.shift_id : null, off: a.mode === 'off', clear: a.mode === 'clear' }
    const r = await run(() => api.hrSend('POST', 'roster', body), (x) => `✓ ${plural(x.changed, 'day')} changed`)
    if (r) load()
  }
  return (
    <HrPage title="Shifts & Roster" sub={tab === 'roster' && d ? `Week of ${fmtDate(d.start)}` : 'Shift timings'}
      actions={tab === 'roster' && <div className="fin-inline">
        <button className="btn icon" onClick={() => setF({ ...f, start: addDays(f.start, -7) })} title="Previous week">‹</button>
        <button className="btn" onClick={() => setF({ ...f, start: mondayOf(todayIso()) })}>This week</button>
        <button className="btn icon" onClick={() => setF({ ...f, start: addDays(f.start, 7) })} title="Next week">›</button></div>}
      side={tab === 'roster' ? <>
        <F label="Department"><Sel value={f.department_id} onChange={set('department_id')} placeholder="Every department"
          options={o.departments.map((x) => ({ value: x.id, label: x.name }))} /></F>
        <F label="Location"><Sel value={f.location} onChange={set('location')} placeholder="Everywhere" options={o.locations} /></F>
        <div className="fin-sidehead">Change the roster · {people(pick.length)} ticked</div>
        <DateRange from={a.date_from} to={a.date_to} setFrom={setAA('date_from')} setTo={setAA('date_to')} />
        <div className="fin-chips">{[['shift', 'Shift'], ['off', 'Day off'], ['clear', 'Back to usual']].map(([k, l]) => (
          <button key={k} className={'fin-chip' + (a.mode === k ? ' on' : '')} onClick={() => setA({ ...a, mode: k })}>{l}</button>))}</div>
        {a.mode === 'shift' && <F label="Shift"><Sel value={a.shift_id} onChange={setAA('shift_id')} placeholder="Choose"
          options={o.shifts.filter((s) => s.active !== false).map((s) => ({ value: s.id, label: `${s.name} · ${s.start_time}–${s.end_time}` }))} /></F>}
        <button className="btn primary block" disabled={busy || !pick.length || !a.date_from || (a.mode === 'shift' && !a.shift_id)} onClick={assign}>Apply</button>
        <div className="small muted fin-note">Each person works their usual shift and weekly off (Job tab) unless the roster says otherwise for a day.</div>
      </> : null}>
      <Tabs tabs={[['roster', 'Weekly roster'], ['shifts', 'Shifts', o.shifts.length]]} value={tab} onChange={setTab} />
      {tab === 'shifts' ? <MasterTable kind="shifts" toast={toast} onChange={reloadOpts} noun="shift"
        cols={[['name', 'Shift'], ['code', 'Code'], [(r) => `${r.start_time}–${r.end_time}`, 'Timing'], [(r) => `${r.break_minutes} min`, 'Break'],
          [(r) => `${r.grace_minutes} min`, 'Grace'], [(r) => `${r.full_day_hours} h / ${r.half_day_hours} h`, 'Full / half day'],
          [(r) => `after ${r.ot_after_minutes} min`, 'Overtime'], ['employees', 'People']]}
        fields={masterDefs(o).shifts.fields} blank={SHIFT_BLANK} />
        : !d ? <Loading>Reading the roster…</Loading> : d.rows.length === 0 ? <Empty>Nobody to roster.</Empty> : (
          <div className="tablewrap fin-scroll"><table className="items hr-roster">
            <thead><tr><th style={{ width: 30 }}><input type="checkbox" checked={pick.length === d.rows.length}
              onChange={() => setPick(pick.length === d.rows.length ? [] : d.rows.map((r) => r.id))} aria-label="Everyone" /></th>
              <th>Employee</th>{d.dates.map((x) => <th key={x.date} className={x.holiday ? 'hol' : ''} title={x.holiday || ''}>{x.dow} <b>{x.d}</b></th>)}</tr></thead>
            <tbody>{d.rows.map((r) => (
              <tr key={r.id}>
                <td><input type="checkbox" checked={pick.includes(r.id)} onChange={() => setPick(pick.includes(r.id) ? pick.filter((x) => x !== r.id) : [...pick, r.id])} /></td>
                <td><b>{r.name}</b><div className="small muted">{r.designation || r.code}</div></td>
                {d.dates.map((x) => {
                  const c = r.days[x.date] || {}
                  const s = shift[c.shift_id]
                  return (
                    <td key={x.date}>{c.out ? <span className="muted">—</span> : c.off ? <span className={'hr-shift off' + (c.set ? ' set' : '')}>OFF</span>
                      : s ? <span className={'hr-shift' + (c.set ? ' set' : '')} style={{ '--sc': s.color }} title={`${s.name} ${s.start_time}–${s.end_time}`}>
                        {s.code || s.name}<small>{s.start_time}–{s.end_time}</small></span> : <span className="muted small">no shift</span>}</td>
                  )
                })}
              </tr>
            ))}</tbody>
          </table></div>
        )}
    </HrPage>
  )
}

// A list of one kind of master with a dialog to add and edit — shifts,
// departments, designations, holidays, leave types.
function MasterTable({ kind, cols, fields, blank, toast, onChange, noun }) {
  const [rows, setRows] = useState(null)
  const [edit, setEdit] = useState(null)
  const [, run] = useRun(toast)
  const load = useCallback(() => api.hrGet(`masters/${kind}`).then(setRows).catch((e) => toast(errText(e), 'err')), [kind, toast])
  useEffect(() => { load() }, [load])
  const del = async (x) => {
    if (!window.confirm(`Delete ${x.name}?`)) return
    const r = await run(() => api.hrSend('DELETE', `masters/${kind}/${x.id}`), (y) => (y.deactivated ? `${x.name} is in use — switched off instead` : `${x.name} deleted`))
    if (r) { load(); onChange && onChange() }
  }
  return (
    <>
      <div className="hr-mhead"><span className="small muted">{rows ? plural(rows.length, noun) : ''}</span>
        <button className="btn primary sm" onClick={() => setEdit({ ...blank })}><Icon name="plus" size={13} /> Add {noun}</button></div>
      {!rows ? <Loading>Reading…</Loading> : rows.length === 0 ? <Empty>None yet.</Empty> : (
        <div className="tablewrap fin-scroll"><table className="items fin-table">
          <thead><tr>{cols.map(([, l]) => <th key={l}>{l}</th>)}<th /></tr></thead>
          <tbody>{rows.map((x) => (
            <tr key={x.id} className={'clickable' + (x.active === false ? ' fin-void' : '')} onDoubleClick={() => setEdit(x)}>
              {cols.map(([k, l]) => <td key={l}>{typeof k === 'function' ? k(x) : typeof x[k] === 'boolean' ? (x[k] ? 'Yes' : 'No') : x[k] ?? '—'}</td>)}
              <td className="acts"><button className="btn sm" onClick={() => setEdit(x)}><Icon name="pencil" size={13} /></button>
                <button className="btn sm danger" onClick={() => del(x)}><Icon name="trash" size={13} /></button></td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
      {edit && <MasterEditor kind={kind} noun={noun} fields={fields} value={edit} toast={toast} onClose={() => setEdit(null)}
        onSaved={() => { setEdit(null); load(); onChange && onChange() }} />}
    </>
  )
}

// ==========================================================================
//  Overtime
// ==========================================================================
function Overtime({ toast }) {
  const [f, setF] = useState({ date_from: monthStart(), date_to: todayIso(), status: 'pending' })
  const [rows, setRows] = useState(null)
  const [pick, setPick] = useState([])
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const load = useCallback(() => api.hrGet('overtime', f).then((r) => { setRows(r); setPick([]) }).catch((e) => toast(errText(e), 'err')), [f, toast])
  useEffect(() => { load() }, [load])
  const decide = async (decision, ids = pick, minutes) => {
    const r = await run(() => api.hrSend('POST', 'overtime/decide', { ids, decision, minutes }), (x) => `✓ ${plural(x.changed, 'day')} ${decision}`)
    if (r) load()
  }
  const tot = (rows || []).reduce((s, r) => s + (r.status === 'approved' ? r.amount : 0), 0)
  return (
    <HrPage title="Overtime" sub={rows ? `${plural(rows.length, 'day')} · approved ₹${Math.round(tot).toLocaleString('en-IN')}` : ''}
      actions={<>
        <button className="btn primary" disabled={busy || !pick.length} onClick={() => decide('approved')}><Icon name="clipcheck" size={14} /> Approve {pick.length || ''}</button>
        <button className="btn" disabled={busy || !pick.length} onClick={() => decide('rejected')}>Reject</button></>}
      side={<>
        <DateRange from={f.date_from} to={f.date_to} setFrom={set('date_from')} setTo={set('date_to')} />
        <F label="Status"><Sel value={f.status} onChange={set('status')} placeholder="All"
          options={[{ value: 'pending', label: 'Waiting for approval' }, { value: 'approved', label: 'Approved' }, { value: 'rejected', label: 'Rejected' }]} /></F>
        <div className="small muted fin-note">Overtime is time worked past a full day on the person's shift, once it passes the
          shift's “overtime after” minutes. Only approved hours are paid, at the rate in HR Settings.</div>
      </>}>
      {!rows ? <Loading>Reading…</Loading> : rows.length === 0 ? <Empty>No overtime here.</Empty> : (
        <div className="tablewrap fin-scroll"><table className="items fin-table">
          <thead><tr><th style={{ width: 30 }}><input type="checkbox" checked={pick.length === rows.length}
            onChange={() => setPick(pick.length === rows.length ? [] : rows.map((r) => r.id))} aria-label="All" /></th>
            <th>Date</th><th>Employee</th><th>In – out</th><th className="r">Worked</th><th className="r">Overtime</th>
            <th className="r">Approved</th><th className="r">Rate / h</th><th className="r">Amount</th><th>Status</th><th /></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.id}>
              <td><input type="checkbox" checked={pick.includes(r.id)} onChange={() => setPick(pick.includes(r.id) ? pick.filter((x) => x !== r.id) : [...pick, r.id])} /></td>
              <td>{fmtDate(r.date)}</td><td><b>{r.name}</b><div className="small muted">{r.code}</div></td>
              <td className="mono">{r.in_time}–{r.out_time}</td><td className="r">{hrs(r.worked)} h</td>
              <td className="r b">{hrs(r.ot_minutes)} h</td><td className="r">{r.approved_minutes ? hrs(r.approved_minutes) + ' h' : '—'}</td>
              <td className="r">{money(r.rate)}</td><td className="r">{rs0(r.amount)}</td><td><Tone s={r.status} /></td>
              <td className="acts">{r.status !== 'approved' && <button className="btn sm" onClick={() => {
                const m = window.prompt('Minutes to approve', r.ot_minutes)
                if (m !== null) decide('approved', [r.id], m)
              }}>Approve…</button>}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
    </HrPage>
  )
}

// ==========================================================================
//  Leave
// ==========================================================================
function Leave({ toast }) {
  const [o] = useHrOptions()
  const [tab, setTab] = useState('requests')
  const [f, setF] = useState({ status: 'pending', employee_id: '', date_from: '', date_to: '' })
  const [rows, setRows] = useState(null)
  const [apply, setApply] = useState(false)
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const load = useCallback(() => api.hrGet('leave', f).then(setRows).catch((e) => toast(errText(e), 'err')), [f, toast])
  useEffect(() => { if (tab === 'requests') load() }, [load, tab])
  const decide = async (r, d) => {
    const note = d === 'rejected' ? window.prompt('Why is it refused?', '') : ''
    if (note === null) return
    if (d === 'cancelled' && !window.confirm(`Cancel ${r.number}? ${r.status === 'approved' ? 'The days go back on the balance.' : ''}`)) return
    const res = await run(() => api.hrSend('POST', `leave/${r.id}/${d}`, { note }), `✓ ${r.number} ${d}`)
    if (res) load()
  }
  const pending = (rows || []).filter((r) => r.status === 'pending').length
  return (
    <HrPage title="Leave" sub={tab === 'requests' && rows ? plural(rows.length, 'request') : ''}
      actions={<button className="btn primary" onClick={() => setApply(true)}><Icon name="plus" size={14} /> Apply leave</button>}
      side={tab === 'requests' ? <>
        <F label="Status"><Sel value={f.status} onChange={set('status')} placeholder="All"
          options={[{ value: 'pending', label: 'Waiting for approval' }, { value: 'approved', label: 'Approved' },
            { value: 'rejected', label: 'Rejected' }, { value: 'cancelled', label: 'Cancelled' }]} /></F>
        <F label="Employee"><Sel value={f.employee_id} onChange={set('employee_id')} placeholder="Everyone" options={empOpts(o)} /></F>
        <DateRange from={f.date_from} to={f.date_to} setFrom={set('date_from')} setTo={set('date_to')} />
      </> : null}>
      <Tabs tabs={[['requests', 'Requests', f.status === 'pending' ? pending : 0], ['balances', 'Balances'], ['calendar', 'Calendar'], ['accrual', 'Accrual & year end']]}
        value={tab} onChange={setTab} />
      {tab === 'requests' && (!rows ? <Loading>Reading…</Loading> : rows.length === 0 ? <Empty>No leave requests here.</Empty> : (
        <div className="tablewrap fin-scroll"><table className="items fin-table">
          <thead><tr><th>No</th><th>Employee</th><th>Leave</th><th>Dates</th><th className="r">Days</th><th>Reason</th><th>Status</th><th /></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.id}>
              <td className="mono">{r.number}</td><td><b>{r.employee}</b><div className="small muted">{r.code}</div></td>
              <td><span className="hr-lt" style={{ '--lc': r.color }}>{r.type}</span> <span className="small muted">{r.type_name}</span></td>
              <td>{fmtDate(r.from_date)}{r.to_date !== r.from_date ? ` – ${fmtDate(r.to_date)}` : ''}{r.half_day ? ' (half)' : ''}</td>
              <td className="r b">{r.days}</td><td className="wrap">{r.reason}{r.decision_note && <div className="small muted">“{r.decision_note}” — {r.decided_by}</div>}</td>
              <td><Tone s={r.status} /></td>
              <td className="acts">{r.status === 'pending' && <>
                <button className="btn sm primary" disabled={busy} onClick={() => decide(r, 'approved')}>Approve</button>
                <button className="btn sm" disabled={busy} onClick={() => decide(r, 'rejected')}>Reject</button></>}
                {['pending', 'approved'].includes(r.status) && <button className="btn sm danger" disabled={busy} onClick={() => decide(r, 'cancelled')}>Cancel</button>}</td>
            </tr>
          ))}</tbody>
        </table></div>
      ))}
      {tab === 'balances' && <LeaveBalances toast={toast} />}
      {tab === 'calendar' && <LeaveCalendar toast={toast} />}
      {tab === 'accrual' && <LeaveAccrual toast={toast} />}
      {apply && <ApplyLeave toast={toast} onClose={() => setApply(false)} onDone={() => { setApply(false); setTab('requests'); load() }} />}
    </HrPage>
  )
}

function ApplyLeave({ toast, onClose, onDone, preset }) {
  const [o] = useHrOptions()
  const [f, setF] = useState({ employee_id: '', leave_type_id: '', from_date: todayIso(), to_date: '', half_day: false, reason: '', approve: false, ...(preset || {}) })
  const [bal, setBal] = useState(null)
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  useEffect(() => { if (f.employee_id) api.hrGet('leave-balances', { employee_id: f.employee_id }).then(setBal).catch(() => setBal(null)); else setBal(null) }, [f.employee_id])
  const save = async () => {
    const r = await run(() => api.hrSend('POST', 'leave', { ...f, employee_id: +f.employee_id, leave_type_id: +f.leave_type_id }),
      (x) => `✓ ${x.number} for ${x.days} day(s) — ${x.status}`)
    if (r) onDone()
  }
  return (
    <Modal title="Apply leave" onClose={onClose}
      foot={<><div className="spacer" /><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !f.employee_id || !f.leave_type_id || !f.from_date} onClick={save}>
          {f.approve ? 'Apply & approve' : 'Apply'}</button></>}>
      <div className="fin-form">
        <F label="Employee" req><Sel value={f.employee_id} onChange={set('employee_id')} placeholder="Choose" options={empOpts(o)} /></F>
        <F label="Leave" req><Sel value={f.leave_type_id} onChange={set('leave_type_id')} placeholder="Choose"
          options={o.leave_types.filter((t) => t.active !== false).map((t) => ({ value: t.id, label: `${t.code} — ${t.name}` }))} /></F>
        <DateField label="From" required value={f.from_date} onChange={set('from_date')} />
        <DateField label="To" value={f.to_date} onChange={set('to_date')} />
        <F label="Half day"><label className="fin-check"><input type="checkbox" checked={f.half_day} onChange={set('half_day')} /> Half a day</label></F>
        <F label="Approve now"><label className="fin-check"><input type="checkbox" checked={f.approve} onChange={set('approve')} /> Approve as I apply</label></F>
        <F label="Reason" style={{ gridColumn: '1 / -1' }}><input value={f.reason} onChange={set('reason')} /></F>
      </div>
      {bal && <div className="hr-leavecards small">{bal.map((b) => (
        <div key={b.leave_type_id} className={'hr-leavecard' + (+f.leave_type_id === b.leave_type_id ? ' on' : '')} style={{ '--lc': b.color }}>
          <span>{b.code}</span><b>{b.balance}</b><small>{b.pending ? `${b.pending} pending` : 'in hand'}</small></div>))}</div>}
      <div className="small muted fin-note">Weekly offs and holidays inside the dates are not counted as leave.</div>
    </Modal>
  )
}

function LeaveBalances({ toast }) {
  const [o] = useHrOptions()
  const [eid, setEid] = useState('')
  const [bal, setBal] = useState(null)
  const [ledger, setLedger] = useState(null)
  const [adj, setAdj] = useState({ leave_type_id: '', days: '', kind: 'adjust', remarks: '' })
  const [busy, run] = useRun(toast)
  const load = useCallback(() => {
    if (!eid) return
    api.hrGet('leave-balances', { employee_id: eid }).then(setBal).catch(() => setBal([]))
    api.hrGet(`employees/${eid}/leave-ledger`).then(setLedger).catch(() => setLedger([]))
  }, [eid])
  useEffect(() => { load() }, [load])
  const save = async () => {
    const r = await run(() => api.hrSend('POST', 'leave-adjust', { ...adj, employee_id: +eid, leave_type_id: +adj.leave_type_id, days: +adj.days }), '✓ Balance adjusted')
    if (r) { setAdj({ ...adj, days: '', remarks: '' }); load() }
  }
  return (
    <>
      <div className="fin-toolbar flat"><F label="Employee" style={{ minWidth: 300 }}><Sel value={eid} onChange={setEid} placeholder="Choose someone" options={empOpts(o)} /></F></div>
      {!eid ? <Empty>Choose someone to see their balances and every day added and taken.</Empty> : !bal ? <Loading>Reading…</Loading> : (
        <>
          <div className="hr-leavecards">{bal.map((b) => (
            <div key={b.leave_type_id} className="hr-leavecard" style={{ '--lc': b.color }}>
              <span>{b.code}</span><b>{b.balance}</b><small>{b.name}{b.pending ? ` · ${b.pending} pending` : ''}</small>
              <small className="muted">quota {b.quota} / year · taken {b.taken_this_year}</small></div>))}</div>
          <div className="fin-sidehead" style={{ marginTop: 14 }}>Opening balance or correction</div>
          <div className="fin-form four" style={{ marginTop: 8 }}>
            <F label="Leave"><Sel value={adj.leave_type_id} onChange={(v) => setAdj({ ...adj, leave_type_id: v })} placeholder="Choose"
              options={o.leave_types.map((t) => ({ value: t.id, label: t.code }))} /></F>
            <F label="Days (minus to take away)"><input type="number" step="0.5" value={adj.days} onChange={(e) => setAdj({ ...adj, days: e.target.value })} /></F>
            <F label="As"><Sel value={adj.kind} onChange={(v) => setAdj({ ...adj, kind: v })} allowBlank={false}
              options={[{ value: 'opening', label: 'Opening balance' }, { value: 'adjust', label: 'Correction' }, { value: 'grant', label: 'Grant (e.g. comp-off)' }]} /></F>
            <F label="Note"><input value={adj.remarks} onChange={(e) => setAdj({ ...adj, remarks: e.target.value })} /></F>
            <F label=" "><button className="btn primary" disabled={busy || !adj.leave_type_id || !+adj.days} onClick={save}>Save</button></F>
          </div>
          {ledger && ledger.length > 0 && (
            <div className="tablewrap fin-scroll"><table className="items fin-table">
              <thead><tr><th>Date</th><th>Leave</th><th>What</th><th className="r">Days</th><th className="r">Balance</th><th>Note</th><th>By</th></tr></thead>
              <tbody>{ledger.map((x) => (
                <tr key={x.id}><td>{fmtDate(x.date)}</td><td>{x.type}</td><td>{x.kind}</td>
                  <td className={'r ' + (x.days < 0 ? 'neg' : 'pos')}>{x.days > 0 ? '+' : ''}{x.days}</td><td className="r b">{x.balance}</td>
                  <td className="small">{x.remarks}</td><td className="small muted">{x.created_by}</td></tr>
              ))}</tbody>
            </table></div>
          )}
        </>
      )}
    </>
  )
}

function LeaveCalendar({ toast }) {
  const [month, setMonth] = useState(thisMonth())
  const [d, setD] = useState(null)
  useEffect(() => { api.hrGet('leave-calendar', { month }).then(setD).catch((e) => toast(errText(e), 'err')) }, [month, toast])
  const [y, m] = month.split('-').map(Number)
  const first = new Date(y, m - 1, 1)
  const ndays = new Date(y, m, 0).getDate()
  const lead = (first.getDay() + 6) % 7
  const cells = [...Array(lead).fill(null), ...Array.from({ length: ndays }, (_, i) => `${month}-${String(i + 1).padStart(2, '0')}`)]
  const on = (iso) => (d?.requests || []).filter((r) => r.from_date <= iso && r.to_date >= iso)
  const hol = (iso) => (d?.holidays || []).find((h) => h.date === iso)
  return (
    <>
      <div className="fin-toolbar flat"><MonthField value={month} onChange={setMonth} /></div>
      <div className="hr-cal">
        {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((w) => <div key={w} className="hr-calhead">{w}</div>)}
        {cells.map((iso, i) => !iso ? <div key={'x' + i} className="hr-calday blank" /> : (
          <div key={iso} className={'hr-calday' + (hol(iso) ? ' hol' : '') + (iso === todayIso() ? ' today' : '')}>
            <span className="hr-caln">{+iso.slice(8)}</span>
            {hol(iso) && <span className="hr-calhol">{hol(iso).name}</span>}
            {on(iso).map((r) => <span key={r.id} className={'hr-calleave' + (r.status === 'pending' ? ' pend' : '')} style={{ '--lc': r.color }}
              title={`${r.employee} · ${r.type_name} · ${r.status}`}>{r.type} · {r.employee}</span>)}
          </div>
        ))}
      </div>
      <div className="small muted fin-note">Dashed: waiting for approval.</div>
    </>
  )
}

function LeaveAccrual({ toast }) {
  const [month, setMonth] = useState(thisMonth())
  const [year, setYear] = useState(String(new Date().getFullYear() - (new Date().getMonth() === 0 ? 1 : 0)))
  const [busy, run] = useRun(toast)
  return (
    <div className="hr-grid two">
      <Card title="Credit a month's leave">
        <p className="small fin-note">Gives everyone employed in the month a twelfth of each <b>monthly</b> leave type's yearly quota
          (pro-rated for someone who joined in it), and on the year's first run the whole quota of each <b>yearly</b> type —
          pro-rated for someone who joined mid-year. Running a month twice credits it once.</p>
        <div className="fin-inline" style={{ alignItems: 'flex-end' }}><MonthField value={month} onChange={setMonth} />
          <button className="btn primary" disabled={busy} onClick={() => run(() => api.hrSend('POST', 'leave-accrual', { month }),
            (x) => `✓ ${plural(x.credited, 'credit')} for ${monthName(x.month)}`)}>Credit</button></div>
      </Card>
      <Card title="Close a year">
        <p className="small fin-note">Carries into the next year what each leave type allows (its carry-forward limit) and lapses
          the rest. Run it once the year's last leave is approved.</p>
        <div className="fin-inline" style={{ alignItems: 'flex-end' }}><F label="Year"><input type="number" value={year} onChange={(e) => setYear(e.target.value)} /></F>
          <button className="btn primary" disabled={busy} onClick={() => window.confirm(`Close ${year}? Balances above each type's carry-forward limit lapse.`)
            && run(() => api.hrSend('POST', 'leave-year-end', { year }), (x) => `✓ ${plural(x.lapsed, 'balance')} trimmed`)}>Close year</button></div>
      </Card>
    </div>
  )
}

// ==========================================================================
//  Advances & loans
// ==========================================================================
function Advances({ toast }) {
  const [o] = useHrOptions()
  const [f, setF] = useState({ status: 'open', employee_id: '' })
  const [rows, setRows] = useState(null)
  const [give, setGive] = useState(false)
  const [repay, setRepay] = useState(null)
  const set = binder(setF)
  const load = useCallback(() => api.hrGet('advances', f).then(setRows).catch((e) => toast(errText(e), 'err')), [f, toast])
  useEffect(() => { load() }, [load])
  const bal = (rows || []).reduce((s, r) => s + r.balance, 0)
  return (
    <HrPage title="Advances & Loans" sub={rows ? `${plural(rows.length, 'advance')} · ${rs0(bal)} outstanding` : ''}
      actions={<button className="btn primary" onClick={() => setGive(true)}><Icon name="plus" size={14} /> Give an advance</button>}
      side={<>
        <F label="Status"><Sel value={f.status} onChange={set('status')} placeholder="All"
          options={[{ value: 'open', label: 'Being recovered' }, { value: 'closed', label: 'Recovered in full' }]} /></F>
        <F label="Employee"><Sel value={f.employee_id} onChange={set('employee_id')} placeholder="Everyone" options={empOpts(o)} /></F>
        <div className="small muted fin-note">An advance is paid out as a Payment voucher in the books. Each month's payroll
          takes back its instalment, as far as the salary allows; a cash repayment can be taken here.</div>
      </>}>
      {!rows ? <Loading>Reading…</Loading> : rows.length === 0 ? <Empty>No advances here.</Empty> : (
        <div className="tablewrap fin-scroll"><table className="items fin-table">
          <thead><tr><th>No</th><th>Employee</th><th>Kind</th><th>Given</th><th className="r">Amount</th><th className="r">Instalment</th>
            <th>From</th><th className="r">Recovered</th><th className="r">Balance</th><th>Voucher</th><th>Status</th><th /></tr></thead>
          <tbody>{rows.map((a) => (
            <tr key={a.id}><td className="mono">{a.number}</td><td><b>{a.employee}</b><div className="small muted">{a.code}{a.reason ? ` · ${a.reason}` : ''}</div></td>
              <td>{a.kind}</td><td>{fmtDate(a.issued_on)}</td><td className="r">{rs0(a.amount)}</td><td className="r">{rs0(a.emi)}</td>
              <td>{monthName(a.recover_from)}</td><td className="r">{rs0(a.recovered)}</td><td className="r b">{rs0(a.balance)}</td>
              <td className="mono small">{a.voucher_no || '—'}</td><td><Tone s={a.status} /></td>
              <td className="acts">{a.status === 'open' && <button className="btn sm" onClick={() => setRepay(a)}>Repaid in cash</button>}</td></tr>
          ))}</tbody>
        </table></div>
      )}
      {give && <GiveAdvance toast={toast} onClose={() => setGive(false)} onDone={() => { setGive(false); load() }} />}
      {repay && <RepayAdvance a={repay} toast={toast} onClose={() => setRepay(null)} onDone={() => { setRepay(null); load() }} />}
    </HrPage>
  )
}

function GiveAdvance({ toast, onClose, onDone }) {
  const [o] = useHrOptions()
  const [f, setF] = useState({ employee_id: '', kind: 'Advance', amount: '', emi: '', issued_on: todayIso(), recover_from: thisMonth(),
    mode: 'Cash', ledger_id: '', reason: '', post: true })
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const months = f.amount && f.emi ? Math.ceil(+f.amount / +f.emi) : null
  const save = async () => {
    const r = await run(() => api.hrSend('POST', 'advances', { ...f, employee_id: +f.employee_id, amount: +f.amount, emi: +f.emi || null,
      ledger_id: f.ledger_id ? +f.ledger_id : null }), (x) => `✓ ${x.number} given`)
    if (r) onDone()
  }
  return (
    <Modal title="Give an advance" onClose={onClose}
      foot={<><div className="spacer" /><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !f.employee_id || !+f.amount} onClick={save}>Give</button></>}>
      <div className="fin-form">
        <F label="Employee" req><Sel value={f.employee_id} onChange={set('employee_id')} placeholder="Choose" options={empOpts(o)} /></F>
        <F label="Kind"><Sel value={f.kind} onChange={set('kind')} allowBlank={false} options={['Advance', 'Loan']} /></F>
        <F label="Amount" req><input type="number" value={f.amount} onChange={set('amount')} /></F>
        <F label="Instalment a month" hint={months ? `${plural(months, 'month')}` : 'Blank: all in one month'}><input type="number" value={f.emi} onChange={set('emi')} /></F>
        <DateField label="Given on" value={f.issued_on} onChange={set('issued_on')} />
        <MonthField label="Recover from" value={f.recover_from} onChange={set('recover_from')} />
        <F label="Paid by"><Sel value={f.mode} onChange={set('mode')} allowBlank={false} options={['Cash', 'NEFT', 'UPI', 'Cheque']} /></F>
        <F label="From (bank / cash)"><Sel value={f.ledger_id} onChange={set('ledger_id')} placeholder={f.mode === 'Cash' ? 'Cash' : 'Bank (default)'}
          options={o.banks.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <F label="Reason" style={{ gridColumn: '1 / -1' }}><input value={f.reason} onChange={set('reason')} /></F>
        <F label="Books"><label className="fin-check"><input type="checkbox" checked={f.post} onChange={set('post')} /> Post the payment in Finance</label></F>
      </div>
    </Modal>
  )
}

function RepayAdvance({ a, toast, onClose, onDone }) {
  const [o] = useHrOptions()
  const [f, setF] = useState({ amount: a.balance, date: todayIso(), mode: 'Cash', ledger_id: '', post: true })
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const save = async () => {
    const r = await run(() => api.hrSend('POST', `advances/${a.id}/repay`, { ...f, amount: +f.amount, ledger_id: f.ledger_id ? +f.ledger_id : null }), '✓ Repayment taken')
    if (r) onDone()
  }
  return (
    <Modal title={`Repayment of ${a.number} — ${a.employee}`} onClose={onClose}
      foot={<><span className="small muted">Balance {rs0(a.balance)}</span><div className="spacer" /><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !+f.amount} onClick={save}>Take</button></>}>
      <div className="fin-form">
        <F label="Amount"><input type="number" value={f.amount} onChange={set('amount')} /></F>
        <DateField label="Date" value={f.date} onChange={set('date')} />
        <F label="Mode"><Sel value={f.mode} onChange={set('mode')} allowBlank={false} options={['Cash', 'UPI', 'NEFT', 'Cheque']} /></F>
        <F label="Into"><Sel value={f.ledger_id} onChange={set('ledger_id')} placeholder={f.mode === 'Cash' ? 'Cash' : 'Bank (default)'}
          options={o.banks.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <F label="Books"><label className="fin-check"><input type="checkbox" checked={f.post} onChange={set('post')} /> Post the receipt in Finance</label></F>
      </div>
    </Modal>
  )
}

// ==========================================================================
//  Sales incentive
// ==========================================================================
const BLANK_SCHEME = { name: '', basis: 'pct', rate: '', slabs: [{ from: 0, to: '', pct: '' }], active: true, notes: '', valid_from: '', valid_to: '',
  filters: { categories: '', brands: '', barcodes: '', locations: [], age_min: '', age_max: '', price_min: '', price_max: '' },
  applies_to: { employees: [], designations: [] } }

function Incentives({ toast }) {
  const [o] = useHrOptions()
  const [tab, setTab] = useState('calc')
  const [schemes, setSchemes] = useState(null)
  const [edit, setEdit] = useState(null)
  const [f, setF] = useState({ date_from: monthStart(), date_to: todayIso() })
  const [d, setD] = useState(null)
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const loadSchemes = useCallback(() => api.hrGet('incentive-schemes').then(setSchemes).catch((e) => toast(errText(e), 'err')), [toast])
  const calc = useCallback(() => { setD(null); api.hrGet('incentives', f).then(setD).catch((e) => toast(errText(e), 'err')) }, [f, toast])
  useEffect(() => { loadSchemes() }, [loadSchemes])
  useEffect(() => { if (tab === 'calc') calc() }, [tab]) // eslint-disable-line react-hooks/exhaustive-deps
  const del = async (s) => {
    if (!window.confirm(`Delete the scheme “${s.name}”?`)) return
    await run(() => api.hrSend('DELETE', `incentive-schemes/${s.id}`), 'Scheme deleted')
    loadSchemes()
  }
  const describe = (s) => {
    const fl = s.filters || {}
    const bits = []
    if (fl.categories?.length) bits.push(`categories ${fl.categories.join(', ')}`)
    if (fl.brands?.length) bits.push(`brands ${fl.brands.join(', ')}`)
    if (fl.barcodes?.length) bits.push(`${plural(fl.barcodes.length, 'barcode')}`)
    if (fl.age_min != null || fl.age_max != null) bits.push(`stock ${fl.age_min != null ? fl.age_min : 0}–${fl.age_max != null ? fl.age_max : '∞'} days old`)
    if (fl.price_min != null || fl.price_max != null) bits.push(`price ₹${fl.price_min ?? 0}–${fl.price_max ?? '∞'}`)
    if (fl.locations?.length) bits.push(fl.locations.join(', '))
    return bits.join(' · ') || 'every line'
  }
  const pays = (s) => (s.basis === 'pct' ? `${s.rate}% of value` : s.basis === 'per_piece' ? `₹${s.rate} a piece`
    : s.slabs.map((x) => `${x.pct}% from ₹${(+x.from).toLocaleString('en-IN')}`).join(', '))
  const cols = d ? [['code', 'Code'], ['name', 'Name'], ['bills', 'Bills'], ['qty', 'Qty'], ['sales', 'Sales'],
    ...d.schemes.map((s) => [(r) => r.by_scheme[s.id] || 0, s.name]), ['incentive', 'Incentive']] : []
  return (
    <HrPage title="Sales Incentive" sub={tab === 'calc' && d?.available ? `${fmtDate(d.from)} – ${fmtDate(d.to)} · ${rs0(d.totals.incentive)} on ${rs0(d.totals.sales)}` : 'Schemes'}
      actions={tab === 'schemes' ? <button className="btn primary" onClick={() => setEdit({ ...BLANK_SCHEME })}><Icon name="plus" size={14} /> New scheme</button>
        : <><button className="btn" disabled={!d?.rows?.length} onClick={() => downloadCsv('incentive', cols, d.rows)}><Icon name="spreadsheet" size={14} /> Export</button>
          <button className="btn primary" onClick={calc}><Icon name="calc" size={14} /> Work out</button></>}
      side={tab === 'calc' ? <>
        <DateRange from={f.date_from} to={f.date_to} setFrom={set('date_from')} setTo={set('date_to')} />
        <div className="small muted fin-note">Read from the Store's bill lines, by the salesperson on each line (else the bill's).
          Every active scheme a line matches pays on it. A month's payroll adds the month's incentive to each payslip by itself.</div>
      </> : null}>
      <Tabs tabs={[['calc', 'Who earned what'], ['schemes', 'Schemes', schemes?.filter((s) => s.active).length]]} value={tab} onChange={setTab} />
      {tab === 'schemes' && (!schemes ? <Loading>Reading…</Loading> : schemes.length === 0 ? (
        <Empty>No schemes yet. A scheme pays a % of what is sold, a fixed amount a piece, or a % that rises with the month's
          sales — on everything, or only on chosen categories, brands, barcodes, price bands or stock older than so many days.
          <button className="btn" onClick={() => setEdit({ ...BLANK_SCHEME })}>Make one</button></Empty>
      ) : (
        <div className="hr-schemes">{schemes.map((s) => (
          <div key={s.id} className={'hr-scheme' + (s.active ? '' : ' off')}>
            <div className="hr-schemehead"><b>{s.name}</b>{!s.active && <Badge tone="draft">off</Badge>}<div className="spacer" />
              <button className="btn sm" onClick={() => setEdit({ ...BLANK_SCHEME, ...s, filters: { ...BLANK_SCHEME.filters, ...s.filters,
                categories: (s.filters.categories || []).join(', '), brands: (s.filters.brands || []).join(', '), barcodes: (s.filters.barcodes || []).join('\n') },
                slabs: s.slabs.length ? s.slabs : BLANK_SCHEME.slabs })}><Icon name="pencil" size={13} /></button>
              <button className="btn sm danger" onClick={() => del(s)}><Icon name="trash" size={13} /></button></div>
            <div className="hr-schemepay">{pays(s)}</div>
            <div className="small">on {describe(s)}</div>
            <div className="small muted">{s.applies_to?.employees?.length || s.applies_to?.designations?.length ? 'for chosen people' : 'for everyone'}
              {s.valid_from || s.valid_to ? ` · ${fmtDate(s.valid_from, 'always')} – ${fmtDate(s.valid_to, 'open')}` : ''}</div>
          </div>
        ))}</div>
      ))}
      {tab === 'calc' && (!d ? <Loading>Reading the till…</Loading> : !d.available ? <Empty>The Store's database cannot be read from here.</Empty> : (
        <>
          <div className="fin-stats"><Stat label="Sales by linked staff" value={rs0(d.totals.sales)} />
            <Stat label="Incentive" value={rs0(d.totals.incentive)} accent="money" />
            <Stat label="Schemes running" value={d.schemes.length} accent="count" /></div>
          {d.unlinked.length > 0 && <div className="fin-warn">Sales by Store logins not linked to an employee earn nothing here:{' '}
            {d.unlinked.map((u) => `${u.username} (${rs0(u.sales)})`).join(', ')}. Link them on the employee's Job tab.</div>}
          {d.rows.length === 0 ? <Empty>No sales by linked staff in this period.</Empty> : (
            <div className="tablewrap fin-scroll"><table className="items fin-table">
              <thead><tr><th>Employee</th><th className="r">Bills</th><th className="r">Pieces</th><th className="r">Sales</th>
                {d.schemes.map((s) => <th key={s.id} className="r" title={s.name}>{s.name}</th>)}<th className="r">Incentive</th></tr></thead>
              <tbody>{d.rows.map((r) => (
                <tr key={r.employee_id}><td><b>{r.name}</b><div className="small muted">{r.code} · {r.designation || '—'}</div></td>
                  <td className="r">{r.bills}</td><td className="r">{r.qty}</td><td className="r">{rs0(r.sales)}</td>
                  {d.schemes.map((s) => <td key={s.id} className="r">{r.by_scheme[s.id] ? rs0(r.by_scheme[s.id]) : <span className="muted">—</span>}</td>)}
                  <td className="r b">{rs0(r.incentive)}</td></tr>
              ))}</tbody>
            </table></div>
          )}
        </>
      ))}
      {edit && <SchemeForm init={edit} toast={toast} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); loadSchemes(); if (tab === 'calc') calc() }} />}
    </HrPage>
  )
}

function SchemeForm({ init, toast, onClose, onSaved }) {
  const [o] = useHrOptions()
  const [f, setF] = useState(init)
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const setFl = (k) => (v) => setF((p) => ({ ...p, filters: { ...p.filters, [k]: v?.target ? v.target.value : v } }))
  const setAp = (k) => (v) => setF((p) => ({ ...p, applies_to: { ...p.applies_to, [k]: v } }))
  const setSlab = (i, k, v) => setF((p) => ({ ...p, slabs: p.slabs.map((s, j) => (j === i ? { ...s, [k]: v } : s)) }))
  const save = async () => {
    const r = await run(() => (f.id ? api.hrSend('PUT', `incentive-schemes/${f.id}`, f) : api.hrSend('POST', 'incentive-schemes', f)), `✓ ${f.name} saved`)
    if (r) onSaved()
  }
  return (
    <Modal wide title={f.id ? `Scheme — ${init.name}` : 'New incentive scheme'} onClose={onClose}
      foot={<><div className="spacer" /><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !f.name} onClick={save}>Save scheme</button></>}>
      <div className="fin-form">
        <h5>What it pays</h5>
        <F label="Name" req><input autoFocus value={f.name} onChange={set('name')} placeholder="e.g. Ageing stock push" /></F>
        <F label="Pays"><Sel value={f.basis} onChange={set('basis')} allowBlank={false} options={o.incentive_bases.map(([v, l]) => ({ value: v, label: l }))} /></F>
        {f.basis !== 'slab' && <F label={f.basis === 'pct' ? 'Rate %' : '₹ a piece'} req><input type="number" step="0.01" value={f.rate} onChange={set('rate')} /></F>}
        <F label="In force"><label className="fin-check"><input type="checkbox" checked={!!f.active} onChange={set('active')} /> Active</label></F>
        {f.basis === 'slab' && <div style={{ gridColumn: '1 / -1' }}>
          <table className="items fin-table fin-entry"><thead><tr><th>Monthly sales from ₹</th><th>up to ₹ (blank: no limit)</th><th>Pays % on all of it</th><th /></tr></thead>
            <tbody>{f.slabs.map((s, i) => (
              <tr key={i}><td><input type="number" value={s.from} onChange={(e) => setSlab(i, 'from', e.target.value)} /></td>
                <td><input type="number" value={s.to ?? ''} onChange={(e) => setSlab(i, 'to', e.target.value)} /></td>
                <td><input type="number" step="0.01" value={s.pct} onChange={(e) => setSlab(i, 'pct', e.target.value)} /></td>
                <td><button className="rowdel" onClick={() => setF({ ...f, slabs: f.slabs.filter((_, j) => j !== i) })}>×</button></td></tr>
            ))}</tbody></table>
          <button className="btn sm" onClick={() => setF({ ...f, slabs: [...f.slabs, { from: '', to: '', pct: '' }] })}><Icon name="plus" size={13} /> Add slab</button>
        </div>}
        <h5>On which lines</h5>
        <F label="Categories" hint="Comma-separated; blank = all"><input value={f.filters.categories} onChange={setFl('categories')} placeholder="SAREE, KURTI" /></F>
        <F label="Brands" hint="Comma-separated; blank = all"><input value={f.filters.brands} onChange={setFl('brands')} /></F>
        <F label="Stock age from (days)" hint="Days from purchase to sale"><input type="number" value={f.filters.age_min ?? ''} onChange={setFl('age_min')} /></F>
        <F label="…to (days)"><input type="number" value={f.filters.age_max ?? ''} onChange={setFl('age_max')} /></F>
        <F label="Price from ₹" hint="Per piece, before tax"><input type="number" value={f.filters.price_min ?? ''} onChange={setFl('price_min')} /></F>
        <F label="…to ₹"><input type="number" value={f.filters.price_max ?? ''} onChange={setFl('price_max')} /></F>
        <F label="Barcodes / SKUs" hint="One per line or comma-separated; blank = all" style={{ gridColumn: '1 / -1' }}>
          <textarea rows={3} value={f.filters.barcodes} onChange={setFl('barcodes')} /></F>
        <DateField label="Valid from" value={f.valid_from || ''} onChange={set('valid_from')} />
        <DateField label="Valid to" value={f.valid_to || ''} onChange={set('valid_to')} />
        <h5>Who earns</h5>
      </div>
      <div className="hr-three">
        <CheckList title="Stores (blank: all)" items={o.locations} value={f.filters.locations || []} onChange={setFl('locations')} />
        <CheckList title="Designations (blank: all)" items={o.designations.map((d) => ({ value: d.id, label: d.name }))}
          value={f.applies_to.designations} onChange={setAp('designations')} />
        <CheckList title="People (blank: all)" items={o.employees.map((e) => ({ value: e.id, label: e.name }))}
          value={f.applies_to.employees} onChange={setAp('employees')} />
      </div>
    </Modal>
  )
}

// ==========================================================================
//  Payroll
// ==========================================================================
const EARN_LABEL = { basic: 'Basic', da: 'DA', hra: 'HRA', conveyance: 'Conveyance', special: 'Special allowance', other: 'Other allowance',
  ot: 'Overtime', incentive: 'Sales incentive', bonus: 'Bonus', arrears: 'Arrears' }
const DED_LABEL = { pf: 'Provident Fund', esi: 'ESI', pt: 'Professional Tax', tds: 'Income tax (TDS)', advance: 'Advance recovery', other: 'Other deduction' }

function Payroll({ toast }) {
  const [o] = useHrOptions()
  const [month, setMonth] = useState(lastMonth())
  const [runs, setRuns] = useState([])
  const [d, setD] = useState(null)
  const [slip, setSlip] = useState(null)
  const [pay, setPay] = useState(false)
  const [q, setQ] = useState('')
  const [busy, run] = useRun(toast)
  const loadRuns = useCallback(() => api.hrGet('payroll').then(setRuns).catch(() => {}), [])
  const load = useCallback(() => api.hrGet(`payroll/${month}`).then(setD).catch((e) => toast(errText(e), 'err')), [month, toast])
  useEffect(() => { setD(null); load() }, [load])
  useEffect(() => { loadRuns() }, [loadRuns])
  const act = async (path, msg, confirm) => {
    if (confirm && !window.confirm(confirm)) return
    const r = await run(() => api.hrSend('POST', `payroll/${month}/${path}`), msg)
    if (r) { setD(r); loadRuns() }
  }
  const t = d?.totals || {}
  const slips = (d?.slips || []).filter((s) => !q || `${s.name} ${s.code}`.toLowerCase().includes(q.toLowerCase()))
  const steps = [['draft', 'Worked out'], ['approved', 'Approved & posted'], ['paid', 'Paid']]
  const at = steps.findIndex(([k]) => k === d?.status)
  const cols = [['code', 'Code'], ['name', 'Name'], ['paid_days', 'Paid days'], ['lop_days', 'LOP'], ['gross', 'Gross'],
    [(s) => s.deductions.pf || 0, 'PF'], [(s) => s.deductions.esi || 0, 'ESI'], [(s) => s.deductions.pt || 0, 'PT'],
    [(s) => s.deductions.tds || 0, 'TDS'], [(s) => s.deductions.advance || 0, 'Advance'], [(s) => s.deductions.other || 0, 'Other'],
    ['total_deductions', 'Deductions'], ['net', 'Net']]
  return (
    <HrPage title="Payroll" sub={d ? `${monthName(month)}${d.status ? ' · ' + d.status : ' · not worked out'}` : ''}
      actions={<>
        {(!d?.status || d.status === 'draft') && <button className="btn" disabled={busy} onClick={() => act('compute', '✓ Worked out')}>
          <Icon name="calc" size={14} /> {d?.status ? 'Work out again' : 'Work out the month'}</button>}
        {d?.status === 'draft' && <button className="btn primary" disabled={busy} onClick={() => act('approve', '✓ Approved and posted to the books',
          `Approve ${monthName(month)}? It locks the month's attendance and leave, and posts the salaries to Finance.`)}>
          <Icon name="clipcheck" size={14} /> Approve & post</button>}
        {d?.status === 'approved' && <>
          <button className="btn" disabled={busy} onClick={() => act('reopen', 'Reopened', 'Reopen the month? Its journal is cancelled in the books.')}><Icon name="undo" size={14} /> Reopen</button>
          <button className="btn primary" disabled={busy} onClick={() => setPay(true)}><Icon name="rupee" size={14} /> Pay salaries</button></>}
        {d?.status && <a className="btn" href={`/api/hr/payroll/${month}/bank-file`}><Icon name="upload" size={14} /> Bank file</a>}
        {d?.slips?.length > 0 && <button className="btn" onClick={() => downloadCsv(`payroll-${month}`, cols, d.slips)}><Icon name="spreadsheet" size={14} /> Register</button>}
        {d?.slips?.length > 0 && <button className="btn" onClick={() => printSlips(d.slips.map((s) => s.id), toast)}><Icon name="printer" size={14} /> All payslips</button>}</>}
      side={<>
        <MonthField value={month} onChange={setMonth} />
        <F label="Find"><input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name or code" /></F>
        <div className="fin-sidehead">Months</div>
        <div className="hr-runlist">{runs.length === 0 ? <div className="small muted">None worked out yet.</div> : runs.map((r) => (
          <button key={r.month} className={'hr-runrow' + (r.month === month ? ' on' : '')} onClick={() => setMonth(r.month)}>
            <span>{new Date(r.month + '-01').toLocaleString('en-IN', { month: 'short', year: 'numeric' })}</span>
            <Tone s={r.status} /><b>{rs0(r.totals?.net)}</b></button>))}</div>
        <div className="small muted fin-note">Paid days come from Attendance and Leave; overtime from approved hours; incentive from
          the Store's bills; advances from their instalments. Bonus, arrears, a deduction or a hold are added on a payslip.</div>
      </>}>
      {!d ? <Loading>Reading…</Loading> : !d.status ? (
        <Empty>{monthName(month)} has not been worked out. <button className="btn primary" disabled={busy} onClick={() => act('compute', '✓ Worked out')}>Work it out</button></Empty>
      ) : (
        <>
          <div className="hr-steps">{steps.map(([k, l], i) => <span key={k} className={i < at ? 'done' : i === at ? 'on' : ''}><i>{i + 1}</i>{l}</span>)}
            {d.journal && <span className="small muted">Journal {d.journal.voucher_no}{d.journal.status === 'cancelled' ? ' (cancelled)' : ''}</span>}
            {d.payment && <span className="small muted">Payment {d.payment.voucher_no} on {fmtDate(d.paid_on)}</span>}</div>
          <div className="fin-stats hr-stats4">
            <Stat label="People" value={t.employees} accent="count" /><Stat label="Gross" value={rs0(t.gross)} />
            <Stat label="Deductions" value={rs0(t.deductions)} accent="back" /><Stat label="Net pay" value={rs0(t.net)} accent="money" />
            <Stat label="PF (both shares)" value={rs0((t.pf || 0) + (t.employer_pf || 0))} /><Stat label="ESI (both shares)" value={rs0((t.esi || 0) + (t.employer_esi || 0))} />
            <Stat label="TDS" value={rs0(t.tds)} /><Stat label="Cost to company" value={rs0(t.cost)} accent="adjust" />
          </div>
          {t.held > 0 && <div className="fin-warn">{plural(t.held, 'payslip')} on hold — left out of the bank file and the payment.</div>}
          <div className="tablewrap fin-scroll"><table className="items fin-table">
            <thead><tr><th>Employee</th><th className="r">Paid days</th><th className="r">LOP</th><th className="r">OT h</th><th className="r">Gross</th>
              <th className="r">PF</th><th className="r">ESI</th><th className="r">PT</th><th className="r">TDS</th><th className="r">Advance</th>
              <th className="r">Other</th><th className="r">Net</th><th /></tr></thead>
            <tbody>{slips.map((s) => (
              <tr key={s.id} className="clickable" onDoubleClick={() => setSlip(s)}>
                <td><b>{s.name}</b>{s.adjustments.hold && <Badge tone="pending">hold</Badge>}<div className="small muted">{s.code} · {s.designation || '—'}</div></td>
                <td className="r">{s.paid_days}</td><td className={'r' + (s.lop_days ? ' neg' : '')}>{s.lop_days || ''}</td><td className="r">{s.ot_hours || ''}</td>
                <td className="r">{rs0(s.gross)}</td><td className="r">{s.deductions.pf ? rs0(s.deductions.pf) : ''}</td>
                <td className="r">{s.deductions.esi ? rs0(s.deductions.esi) : ''}</td><td className="r">{s.deductions.pt ? rs0(s.deductions.pt) : ''}</td>
                <td className="r">{s.deductions.tds ? rs0(s.deductions.tds) : ''}</td><td className="r">{s.deductions.advance ? rs0(s.deductions.advance) : ''}</td>
                <td className="r">{s.deductions.other ? rs0(s.deductions.other) : ''}</td><td className="r b">{rs0(s.net)}</td>
                <td className="acts"><button className="btn sm" onClick={() => setSlip(s)}>Open</button></td>
              </tr>
            ))}</tbody>
            <tfoot><tr><td>Total · {slips.length}</td><td /><td /><td /><td className="r">{rs0(slips.reduce((a, s) => a + s.gross, 0))}</td>
              {['pf', 'esi', 'pt', 'tds', 'advance', 'other'].map((k) => <td key={k} className="r">{rs0(slips.reduce((a, s) => a + (s.deductions[k] || 0), 0))}</td>)}
              <td className="r">{rs0(slips.reduce((a, s) => a + s.net, 0))}</td><td /></tr></tfoot>
          </table></div>
        </>
      )}
      {slip && <SlipModal s={slip} draft={d?.status === 'draft'} toast={toast} onClose={() => setSlip(null)}
        onSaved={() => { setSlip(null); load(); loadRuns() }} />}
      {pay && <PayModal month={month} total={t.net} held={t.held} banks={o.banks} toast={toast} onClose={() => setPay(false)}
        onDone={(r) => { setPay(false); setD(r); loadRuns() }} />}
    </HrPage>
  )
}

function SlipModal({ s, draft, toast, onClose, onSaved }) {
  const [f, setF] = useState({ bonus: s.adjustments.bonus || '', arrears: s.adjustments.arrears || '',
    other_deduction: s.adjustments.other_deduction || '', hold: !!s.adjustments.hold, remarks: s.adjustments.remarks || '' })
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const save = async () => {
    const r = await run(() => api.hrSend('PUT', `payslips/${s.id}`, { ...f, bonus: +f.bonus || 0, arrears: +f.arrears || 0,
      other_deduction: +f.other_deduction || 0 }), '✓ Payslip updated')
    if (r) onSaved()
  }
  return (
    <Modal wide title={`${s.name} · ${s.code}`} onClose={onClose}
      foot={<><button className="btn" onClick={() => printSlips([s.id], toast)}><Icon name="printer" size={14} /> Payslip</button>
        <div className="spacer" /><button className="btn" onClick={onClose}>Close</button>
        {draft && <button className="btn primary" disabled={busy} onClick={save}>Save & work out again</button>}</>}>
      <div className="hr-slipgrid">
        <div><div className="fin-sidehead">Days</div>
          {[['Days in month', s.days_in_month], ['Employed', s.payable_days], ['Present', s.present_days], ['Paid leave', s.leave_days],
            ['Loss of pay', s.lop_days], ['Paid days', s.paid_days], ['Overtime hours', s.ot_hours]].map(([k, v]) => <div key={k} className="fin-bigfig"><span>{k}</span><b>{v || 0}</b></div>)}</div>
        <div><div className="fin-sidehead">Earnings</div>
          {Object.entries(s.earnings).map(([k, v]) => <div key={k} className="fin-bigfig"><span>{EARN_LABEL[k] || k}</span><b>{rs0(v)}</b></div>)}
          <div className="fin-bigfig strong"><span>Gross</span><b>{rs0(s.gross)}</b></div></div>
        <div><div className="fin-sidehead">Deductions</div>
          {Object.entries(s.deductions).map(([k, v]) => <div key={k} className="fin-bigfig"><span>{DED_LABEL[k] || k}</span><b>{rs0(v)}</b></div>)}
          <div className="fin-bigfig strong"><span>Total</span><b>{rs0(s.total_deductions)}</b></div>
          <div className="fin-bigfig strong net"><span>Net pay</span><b>{rs0(s.net)}</b></div>
          {Object.keys(s.employer).length > 0 && <div className="small muted" style={{ marginTop: 6 }}>Employer also pays:{' '}
            {Object.entries(s.employer).map(([k, v]) => `${k.toUpperCase()} ${rs0(v)}`).join(' · ')}</div>}</div>
      </div>
      {draft ? (
        <>
          <div className="fin-sidehead" style={{ marginTop: 14 }}>Added by hand · kept when the month is worked out again</div>
          <div className="fin-form four" style={{ marginTop: 8 }}>
            <F label="Bonus"><input type="number" value={f.bonus} onChange={set('bonus')} /></F>
            <F label="Arrears"><input type="number" value={f.arrears} onChange={set('arrears')} /></F>
            <F label="Other deduction"><input type="number" value={f.other_deduction} onChange={set('other_deduction')} /></F>
            <F label="Hold"><label className="fin-check"><input type="checkbox" checked={f.hold} onChange={set('hold')} /> Hold the salary</label></F>
            <F label="Remarks" style={{ gridColumn: '1 / -1' }}><input value={f.remarks} onChange={set('remarks')} /></F>
          </div>
        </>
      ) : <div className="small muted fin-note">The month is approved — reopen it to change a payslip.</div>}
    </Modal>
  )
}

function PayModal({ month, total, held, banks, toast, onClose, onDone }) {
  const [f, setF] = useState({ date: todayIso(), mode: 'NEFT', ledger_id: '', ref_no: '' })
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const save = async () => {
    const r = await run(() => api.hrSend('POST', `payroll/${month}/pay`, { ...f, ledger_id: f.ledger_id ? +f.ledger_id : null }), '✓ Salaries paid and posted')
    if (r) onDone(r)
  }
  return (
    <Modal title={`Pay salaries — ${monthName(month)}`} onClose={onClose}
      foot={<><span className="small muted">{rs0(total)}{held ? ` less ${plural(held, 'held payslip')}` : ''}</span>
        <div className="spacer" /><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy} onClick={save}>Mark paid</button></>}>
      <div className="fin-form">
        <DateField label="Paid on" value={f.date} onChange={set('date')} />
        <F label="Mode"><Sel value={f.mode} onChange={set('mode')} allowBlank={false} options={['NEFT', 'RTGS', 'Cheque', 'Cash', 'UPI']} /></F>
        <F label="From"><Sel value={f.ledger_id} onChange={set('ledger_id')} placeholder={f.mode === 'Cash' ? 'Cash' : 'Bank (default)'}
          options={banks.map((b) => ({ value: b.id, label: b.name }))} /></F>
        <F label="Reference / cheque no"><input value={f.ref_no} onChange={set('ref_no')} /></F>
      </div>
      <div className="small muted fin-note">Posts one Payment voucher in Finance: Salary Payable against the bank (or cash). Upload the
        bank file to the bank for the transfers themselves.</div>
    </Modal>
  )
}

// ---- payslips, printed ------------------------------------------------------------
function slipHtml(p) {
  const sn = p.snapshot || {}
  const earn = Object.entries(p.earnings)
  const ded = Object.entries(p.deductions)
  const rows = Math.max(earn.length, ded.length)
  const line = (i) => {
    const e = earn[i]
    const d = ded[i]
    return `<tr><td>${e ? esc(EARN_LABEL[e[0]] || e[0]) : ''}</td><td class="r">${e ? (sn.rates?.[e[0]] ? money(sn.rates[e[0]]) : '') : ''}</td>`
      + `<td class="r">${e ? money(e[1]) : ''}</td><td>${d ? esc(DED_LABEL[d[0]] || d[0]) : ''}</td><td class="r">${d ? money(d[1]) : ''}</td></tr>`
  }
  const kv = (k, v) => `<div class="kv"><span class="muted">${esc(k)}</span><b>${esc(v ?? '—')}</b></div>`
  return `<div class="slip"><div class="hd"><div><h1>${esc(p.company?.name || 'Payslip')}</h1><div class="muted">${esc(p.company?.address || '')}</div></div>`
    + `<div style="text-align:right"><b>Payslip · ${esc(p.label)}</b><div class="muted">${esc(p.status)}</div></div></div>`
    + `<div class="grid">${kv('Employee', `${sn.name} (${sn.code})`)}${kv('Designation', sn.designation)}${kv('Department', sn.department)}`
    + `${kv('Location', sn.location)}${kv('Date of joining', fmtDate(sn.joined_on))}${kv('PAN', sn.pan)}${kv('UAN', sn.uan)}${kv('ESI IP', sn.esi_no)}`
    + `${kv('Bank', [sn.bank_name, sn.bank_account].filter(Boolean).join(' · '))}${kv('Days in month', p.days_in_month)}${kv('Paid days', p.paid_days)}`
    + `${kv('Loss of pay', p.lop_days || 0)}</div>`
    + `<h2>Earnings and deductions</h2><table><thead><tr><th>Earnings</th><th class="r">Rate</th><th class="r">Amount</th><th>Deductions</th><th class="r">Amount</th></tr></thead>`
    + `<tbody>${Array.from({ length: rows }, (_, i) => line(i)).join('')}</tbody>`
    + `<tfoot><tr><th>Gross</th><th></th><th class="r">${money(p.gross)}</th><th>Total deductions</th><th class="r">${money(p.total_deductions)}</th></tr></tfoot></table>`
    + `<div class="net"><span>Net pay</span><span>₹ ${money(p.net)}</span></div><div class="muted" style="margin-top:4px">Rupees ${esc(inWords(p.net))} only</div>`
    + `<div class="grid" style="margin-top:12px">${kv('Gross this financial year', money(p.ytd?.gross))}${kv('TDS this financial year', money(p.ytd?.tds))}`
    + `${kv('PF this financial year', money(p.ytd?.pf))}${kv('Employer PF / ESI this month', Object.entries(p.employer || {}).map(([k, v]) => `${k.toUpperCase()} ${money(v)}`).join(' · ') || '—')}</div>`
    + `${p.adjustments?.remarks ? `<p><b>Remarks:</b> ${esc(p.adjustments.remarks)}</p>` : ''}`
    + `<div class="foot">This is a computer-generated payslip and needs no signature.</div></div>`
}

async function printSlips(ids, toast) {
  try {
    const slips = await Promise.all(ids.map((id) => api.hrGet(`payslips/${id}`)))
    printHtml(slips.length === 1 ? `Payslip ${slips[0].snapshot?.name} ${slips[0].label}` : `Payslips ${slips[0]?.label}`, slips.map(slipHtml).join(''))
  } catch (e) { toast && toast(errText(e), 'err') }
}

function Payslips({ toast }) {
  const [o] = useHrOptions()
  const [mode, setMode] = useState('month')
  const [month, setMonth] = useState(lastMonth())
  const [eid, setEid] = useState('')
  const [rows, setRows] = useState(null)
  const [view, setView] = useState(null)
  useEffect(() => {
    setRows(null)
    setView(null)
    if (mode === 'month') api.hrGet(`payroll/${month}`).then((r) => setRows((r.slips || []).map((s) => ({ ...s, label: monthName(month), status: r.status })))).catch(() => setRows([]))
    else if (eid) api.hrGet(`employees/${eid}/payslips`).then(setRows).catch(() => setRows([]))
    else setRows([])
  }, [mode, month, eid])
  const open = (id) => api.hrGet(`payslips/${id}`).then(setView).catch((e) => toast(errText(e), 'err'))
  return (
    <HrPage title="Payslips" sub={rows ? plural(rows.length, 'payslip') : ''}
      actions={<>
        {view && <button className="btn primary" onClick={() => printSlips([view.id], toast)}><Icon name="printer" size={14} /> Print this</button>}
        {rows?.length > 0 && <button className="btn" onClick={() => printSlips(rows.map((r) => r.id), toast)}><Icon name="printer" size={14} /> Print all {rows.length}</button>}</>}
      side={<>
        <div className="fin-chips">{[['month', 'By month'], ['person', 'By person']].map(([k, l]) => (
          <button key={k} className={'fin-chip' + (mode === k ? ' on' : '')} onClick={() => setMode(k)}>{l}</button>))}</div>
        {mode === 'month' ? <MonthField value={month} onChange={setMonth} />
          : <F label="Employee"><Sel value={eid} onChange={setEid} placeholder="Choose" options={empOpts(o)} /></F>}
        <div className="hr-runlist">{(rows || []).map((r) => (
          <button key={r.id} className={'hr-runrow' + (view?.id === r.id ? ' on' : '')} onClick={() => open(r.id)}>
            <span>{mode === 'month' ? r.name : r.label}</span><b>{rs0(r.net)}</b></button>))}</div>
      </>}>
      {!view ? <Empty>{rows && rows.length === 0 ? (mode === 'month' ? `No payroll for ${monthName(month)} yet — work it out on Payroll.` : 'Choose someone.')
        : 'Pick a payslip on the left to see it here.'}</Empty>
        : <div className="hr-slipview" dangerouslySetInnerHTML={{ __html: slipHtml(view) }} />}
    </HrPage>
  )
}

// ==========================================================================
//  Performance
// ==========================================================================
function Performance({ toast }) {
  const [o] = useHrOptions()
  const [cycles, setCycles] = useState(null)
  const [cid, setCid] = useState(null)
  const [d, setD] = useState(null)
  const [newCycle, setNewCycle] = useState(null)
  const [goal, setGoal] = useState(null)
  const [rev, setRev] = useState(null)
  const [busy, run] = useRun(toast)
  const loadCycles = useCallback(() => api.hrGet('cycles').then((c) => { setCycles(c); setCid((x) => x || c[0]?.id || null) }).catch(() => setCycles([])), [])
  const load = useCallback(() => { if (cid) api.hrGet(`cycles/${cid}/review`).then(setD).catch((e) => toast(errText(e), 'err')) }, [cid, toast])
  useEffect(() => { loadCycles() }, [loadCycles])
  useEffect(() => { setD(null); load() }, [load])
  const saveCycle = async () => {
    const r = await run(() => (newCycle.id ? api.hrSend('PUT', `cycles/${newCycle.id}`, newCycle) : api.hrSend('POST', 'cycles', newCycle)), '✓ Review cycle saved')
    if (r) { setNewCycle(null); setCid(r.id); loadCycles() }
  }
  const stars = (n) => (n ? '★'.repeat(Math.round(n)) + '☆'.repeat(5 - Math.round(n)) : '—')
  return (
    <HrPage title="Performance" sub={d ? `${d.cycle.name} · ${fmtDate(d.cycle.period_from)} – ${fmtDate(d.cycle.period_to)}` : ''}
      actions={<>
        {cid && <button className="btn primary" onClick={() => setGoal({ cycle_id: cid, employee_id: '', title: '', kind: 'KPI', metric: 'manual', weight: '', target: '', unit: '' })}>
          <Icon name="plus" size={14} /> Add goal / KPI</button>}
        <button className="btn" onClick={() => setNewCycle({ name: '', period_from: monthStart(), period_to: todayIso(), status: 'open' })}><Icon name="plus" size={14} /> New cycle</button></>}
      side={<>
        <div className="fin-sidehead">Review cycles</div>
        <div className="hr-runlist">{(cycles || []).length === 0 ? <div className="small muted">No review cycles yet.</div> : cycles.map((c) => (
          <button key={c.id} className={'hr-runrow' + (c.id === cid ? ' on' : '')} onClick={() => setCid(c.id)}>
            <span>{c.name}<small className="muted"> · {c.goals} goals</small></span><Tone s={c.status} /></button>))}</div>
        {d && <button className="btn block" onClick={() => setNewCycle({ ...d.cycle })}>Edit this cycle</button>}
        <div className="small muted fin-note">A KPI measured as Store sales or bills reads its actual from the person's bills in the
          cycle's dates; attendance % from their register. The score weighs each goal's achievement (capped at 150%) by its weight.</div>
      </>}>
      {!cycles ? <Loading>Reading…</Loading> : !cid ? <Empty>Start a review cycle — a quarter, a half or a year — then give people goals and KPIs.
        <button className="btn" onClick={() => setNewCycle({ name: '', period_from: monthStart(), period_to: todayIso(), status: 'open' })}>New cycle</button></Empty>
        : !d ? <Loading>Reading the cycle…</Loading> : d.rows.length === 0 ? <Empty>Nobody has goals in this cycle yet. Add a goal or KPI to begin.</Empty> : (
          <div className="tablewrap fin-scroll"><table className="items fin-table">
            <thead><tr><th>Employee</th><th>Goals &amp; KPIs</th><th className="r">Score</th><th>Suggested</th><th>Rating</th><th>Increment</th><th>Status</th><th /></tr></thead>
            <tbody>{d.rows.map((r) => (
              <tr key={r.id} className="clickable" onDoubleClick={() => setRev(r)}>
                <td><b>{r.name}</b><div className="small muted">{r.code} · {r.designation || '—'}</div></td>
                <td>{r.goals.map((g) => <div key={g.id} className="hr-goalline"><span>{g.title}</span>
                  <span className="hr-meter"><i style={{ width: `${Math.min(100, (g.achievement || 0) / 1.5)}%` }} /></span>
                  <small>{g.achievement != null ? `${g.achievement}%` : '—'}{g.weight ? ` · w${g.weight}` : ''}</small></div>)}</td>
                <td className="r b">{r.score ?? '—'}</td><td className="hr-stars">{stars(r.suggested)}</td>
                <td className="hr-stars">{stars(r.appraisal?.rating)}</td><td>{r.appraisal?.increment_pct ? `${r.appraisal.increment_pct}%` : '—'}</td>
                <td>{r.appraisal ? <Tone s={r.appraisal.status} /> : <span className="muted small">not started</span>}</td>
                <td className="acts"><button className="btn sm" onClick={() => setRev(r)}>Review</button></td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      {newCycle && (
        <Modal title={newCycle.id ? 'Edit review cycle' : 'New review cycle'} onClose={() => setNewCycle(null)}
          foot={<><div className="spacer" /><button className="btn" onClick={() => setNewCycle(null)}>Cancel</button>
            <button className="btn primary" disabled={busy || !newCycle.name} onClick={saveCycle}>Save</button></>}>
          <div className="fin-form">
            <F label="Name" req><input autoFocus value={newCycle.name} onChange={(e) => setNewCycle({ ...newCycle, name: e.target.value })} placeholder="FY 2026-27 · H1" /></F>
            <DateField label="From" required value={newCycle.period_from} onChange={(v) => setNewCycle({ ...newCycle, period_from: v })} />
            <DateField label="To" required value={newCycle.period_to} onChange={(v) => setNewCycle({ ...newCycle, period_to: v })} />
            <F label="Status"><Sel value={newCycle.status} onChange={(v) => setNewCycle({ ...newCycle, status: v })} allowBlank={false}
              options={[{ value: 'open', label: 'Open' }, { value: 'closed', label: 'Closed (actuals frozen)' }]} /></F>
          </div>
        </Modal>
      )}
      {goal && <GoalForm g={goal} o={o} toast={toast} onClose={() => setGoal(null)} onSaved={() => { setGoal(null); load(); loadCycles() }} />}
      {rev && <ReviewModal r={rev} cycle={d.cycle} toast={toast} onClose={() => setRev(null)} onSaved={() => { load() }}
        onGoal={(g) => setGoal(g)} />}
    </HrPage>
  )
}

function GoalForm({ g, o, toast, onClose, onSaved }) {
  const [f, setF] = useState(g)
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const save = async () => {
    const body = { ...f, employee_id: +f.employee_id }
    const r = await run(() => (f.id ? api.hrSend('PUT', `goals/${f.id}`, body) : api.hrSend('POST', 'goals', body)), '✓ Goal saved')
    if (r) onSaved()
  }
  const del = async () => {
    if (!window.confirm('Delete this goal?')) return
    const r = await run(() => api.hrSend('DELETE', `goals/${f.id}`), 'Goal deleted')
    if (r) onSaved()
  }
  return (
    <Modal title={f.id ? `Goal — ${g.title}` : 'New goal or KPI'} onClose={onClose}
      foot={<>{f.id && <button className="btn danger" onClick={del}>Delete</button>}<div className="spacer" />
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !f.title || !f.employee_id} onClick={save}>Save</button></>}>
      <div className="fin-form">
        <F label="Employee" req><Sel value={f.employee_id} onChange={set('employee_id')} placeholder="Choose" disabled={!!f.id} options={empOpts(o)} /></F>
        <F label="Kind"><Sel value={f.kind} onChange={set('kind')} allowBlank={false} options={['KPI', 'Goal']} /></F>
        <F label="Title" req style={{ gridColumn: '1 / -1' }}><input autoFocus value={f.title} onChange={set('title')} placeholder="e.g. Monthly sales" /></F>
        <F label="Measured as"><Sel value={f.metric} onChange={set('metric')} allowBlank={false} options={o.goal_metrics.map(([v, l]) => ({ value: v, label: l }))} /></F>
        <F label="Weight" hint="Share of the score"><input type="number" value={f.weight ?? ''} onChange={set('weight')} /></F>
        <F label="Target"><input type="number" value={f.target ?? ''} onChange={set('target')} /></F>
        <F label="Unit"><input value={f.unit || ''} onChange={set('unit')} placeholder="₹, bills, %, …" /></F>
        {f.metric === 'manual' && <F label="Actual"><input type="number" value={f.actual ?? ''} onChange={set('actual')} /></F>}
        <F label="Self rating (1–5)"><input type="number" min={1} max={5} step="0.5" value={f.self_rating ?? ''} onChange={set('self_rating')} /></F>
        <F label="Manager rating (1–5)"><input type="number" min={1} max={5} step="0.5" value={f.manager_rating ?? ''} onChange={set('manager_rating')} /></F>
        <F label="Comments" style={{ gridColumn: '1 / -1' }}><input value={f.comments || ''} onChange={set('comments')} /></F>
      </div>
    </Modal>
  )
}

function ReviewModal({ r, cycle, toast, onClose, onSaved, onGoal }) {
  const [o] = useHrOptions()
  const a0 = r.appraisal || {}
  const [f, setF] = useState({ rating: a0.rating ?? r.suggested ?? '', increment_pct: a0.increment_pct ?? '', new_designation_id: a0.new_designation_id || '',
    strengths: a0.strengths || '', improvements: a0.improvements || '', comments: a0.comments || '', effective_on: a0.effective_on || todayIso(),
    status: a0.status || 'draft', score: r.score })
  const [ap, setAp] = useState(r.appraisal)
  const [busy, run] = useRun(toast)
  const set = binder(setF)
  const applied = ap?.status === 'applied'
  const save = async (status) => {
    const res = await run(() => api.hrSend('POST', 'appraisals', { ...f, status: status || f.status, cycle_id: cycle.id, employee_id: r.id,
      rating: num(f.rating), increment_pct: num(f.increment_pct), new_designation_id: f.new_designation_id ? +f.new_designation_id : null }),
    status === 'approved' ? '✓ Approved' : '✓ Saved')
    if (res) { setAp(res); setF({ ...f, status: res.status }); onSaved() }
  }
  const apply = async () => {
    if (!window.confirm(`Apply to ${r.name}'s record? ${f.increment_pct ? `Every pay component goes up ${f.increment_pct}%` : ''}${f.new_designation_id ? ' and the designation changes' : ''}, from ${fmtDate(f.effective_on)}.`)) return
    const res = await run(() => api.hrSend('POST', `appraisals/${ap.id}/apply`), '✓ Applied to the employee')
    if (res) { setAp(res); onSaved() }
  }
  const newGross = r.gross * (1 + (+f.increment_pct || 0) / 100)
  return (
    <Modal wide title={`Review — ${r.name} · ${cycle.name}`} onClose={onClose}
      foot={<>{ap && <Tone s={ap.status} />}<div className="spacer" /><button className="btn" onClick={onClose}>Close</button>
        {!applied && <button className="btn" disabled={busy} onClick={() => save()}>Save</button>}
        {!applied && ap?.status !== 'approved' && <button className="btn primary" disabled={busy || !f.rating} onClick={() => save('approved')}>Approve</button>}
        {ap?.status === 'approved' && <button className="btn primary" disabled={busy} onClick={apply}>Apply to the record</button>}</>}>
      <div className="fin-sidehead">Goals &amp; KPIs · score {r.score ?? '—'}</div>
      <div className="tablewrap"><table className="items fin-table">
        <thead><tr><th>Goal</th><th>Measured</th><th className="r">Weight</th><th className="r">Target</th><th className="r">Actual</th>
          <th className="r">Achieved</th><th className="r">Self</th><th className="r">Manager</th><th /></tr></thead>
        <tbody>{r.goals.map((g) => (
          <tr key={g.id}><td><b>{g.title}</b>{g.comments && <div className="small muted">{g.comments}</div>}</td>
            <td className="small">{(o.goal_metrics.find(([v]) => v === g.metric) || [, g.metric])[1]}</td>
            <td className="r">{g.weight ?? ''}</td><td className="r">{g.target != null ? money(g.target) : '—'}</td>
            <td className="r">{g.actual != null ? money(g.actual) : '—'}</td><td className="r b">{g.achievement != null ? `${g.achievement}%` : '—'}</td>
            <td className="r">{g.self_rating ?? ''}</td><td className="r">{g.manager_rating ?? ''}</td>
            <td className="acts">{!applied && <button className="btn sm" onClick={() => onGoal({ ...g })}><Icon name="pencil" size={13} /></button>}</td></tr>
        ))}</tbody>
      </table></div>
      <div className="fin-form" style={{ marginTop: 14 }}>
        <h5>Appraisal</h5>
        <F label="Rating (1–5)" hint={r.suggested ? `Suggested from the score: ${r.suggested}` : ''}>
          <div className="hr-starpick">{[1, 2, 3, 4, 5].map((n) => <button key={n} type="button" disabled={applied} className={+f.rating >= n ? 'on' : ''}
            onClick={() => setF({ ...f, rating: n })} aria-label={`${n} stars`}>★</button>)}</div></F>
        <F label="Increment %" hint={f.increment_pct ? `Gross ${rs0(r.gross)} → ${rs0(newGross)}` : ''}>
          <input type="number" step="0.5" value={f.increment_pct} disabled={applied} onChange={set('increment_pct')} /></F>
        <F label="New designation"><Sel value={f.new_designation_id} onChange={set('new_designation_id')} disabled={applied} placeholder="No change"
          options={o.designations.map((x) => ({ value: x.id, label: x.name }))} /></F>
        <DateField label="Effective from" value={f.effective_on} onChange={set('effective_on')} />
        <F label="Strengths" style={{ gridColumn: '1 / -1' }}><textarea rows={2} value={f.strengths} disabled={applied} onChange={set('strengths')} /></F>
        <F label="To improve" style={{ gridColumn: '1 / -1' }}><textarea rows={2} value={f.improvements} disabled={applied} onChange={set('improvements')} /></F>
        <F label="Manager's comments" style={{ gridColumn: '1 / -1' }}><textarea rows={2} value={f.comments} disabled={applied} onChange={set('comments')} /></F>
      </div>
    </Modal>
  )
}

// ==========================================================================
//  HR Reports
// ==========================================================================
//: The reports, listed here too so the Reports screen can show them without a call.
export const HR_REPORTS = [
  ['register', 'Employee Register', 'People', ['status', 'department_id', 'location']],
  ['joiners_exits', 'Joiners & Exits', 'People', ['date_from', 'date_to']],
  ['role_history', 'Role History', 'People', ['date_from', 'date_to']],
  ['birthdays', 'Birthdays & Anniversaries', 'People', ['month']],
  ['documents_expiry', 'Document Expiry', 'People', ['days']],
  ['contracts_expiry', 'Contract Expiry', 'People', ['days']],
  ['attendance_summary', 'Attendance Summary', 'Time', ['month', 'department_id', 'location']],
  ['late_report', 'Late Arrivals', 'Time', ['date_from', 'date_to']],
  ['overtime', 'Overtime Register', 'Time', ['date_from', 'date_to']],
  ['leave_balance', 'Leave Balances', 'Leave', ['as_on']],
  ['leave_register', 'Leave Register', 'Leave', ['date_from', 'date_to']],
  ['payroll_register', 'Payroll Register', 'Payroll', ['month']],
  ['pf_statement', 'PF Statement (ECR)', 'Payroll', ['month']],
  ['esi_statement', 'ESI Statement', 'Payroll', ['month']],
  ['pt_statement', 'Professional Tax', 'Payroll', ['month']],
  ['tds_statement', 'TDS on Salaries', 'Payroll', ['month']],
  ['bank_transfer', 'Salary Bank Transfer', 'Payroll', ['month']],
  ['cost_by_department', 'Salary Cost by Department', 'Payroll', ['month']],
  ['incentive_report', 'Sales Incentive', 'Payroll', ['date_from', 'date_to']],
  ['advances', 'Advances Outstanding', 'Payroll', []],
]

function HrReports({ toast, report }) {
  const [o] = useHrOptions()
  const [key, setKey] = useState(report || 'register')
  const [p, setP] = useState({ month: lastMonth(), date_from: monthStart(), date_to: todayIso(), status: 'current', department_id: '',
    location: '', days: 60, as_on: todayIso() })
  const [d, setD] = useState(null)
  const def = HR_REPORTS.find((r) => r[0] === key) || HR_REPORTS[0]
  const load = useCallback(() => {
    setD(null)
    const params = Object.fromEntries(def[3].map((k) => [k, p[k]]))
    api.hrGet(`reports/${key}`, params).then(setD).catch((e) => { toast(errText(e), 'err'); setD({ columns: [], rows: [] }) })
  }, [key, p]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (report) setKey(report) }, [report])
  useEffect(() => { load() }, [key]) // eslint-disable-line react-hooks/exhaustive-deps
  const set = (k) => (v) => setP((x) => ({ ...x, [k]: v?.target ? v.target.value : v }))
  const isNum = (c) => d?.rows?.some((r) => typeof r[c] === 'number')
  const groups = [...new Set(HR_REPORTS.map((r) => r[2]))]
  const printIt = () => printHtml(d.title, `<h1>${esc(d.title)}</h1><div class="muted">${esc(d.sub || '')}</div><br><table><thead><tr>${d.columns.map((c) => `<th${isNum(c) ? ' class="r"' : ''}>${esc(c)}</th>`).join('')}</tr></thead>`
    + `<tbody>${d.rows.map((r) => `<tr>${d.columns.map((c) => `<td${typeof r[c] === 'number' ? ' class="r"' : ''}>${esc(typeof r[c] === 'number' ? r[c].toLocaleString('en-IN') : r[c] ?? '')}</td>`).join('')}</tr>`).join('')}</tbody>`
    + (Object.keys(d.totals || {}).length ? `<tfoot><tr>${d.columns.map((c, i) => `<th${d.totals[c] != null ? ' class="r"' : ''}>${d.totals[c] != null ? d.totals[c].toLocaleString('en-IN') : i === 0 ? 'Total' : ''}</th>`).join('')}</tr></tfoot>` : '') + '</table>')
  return (
    <HrPage title={def[1]} sub={d ? `${d.sub || ''}${d.sub ? ' · ' : ''}${plural(d.rows.length, 'row')}` : ''}
      actions={<>
        <button className="btn" disabled={!d?.rows?.length} onClick={() => downloadCsv(key, d.columns.map((c) => [c, c]), d.rows)}><Icon name="spreadsheet" size={14} /> CSV</button>
        <button className="btn" disabled={!d?.rows?.length} onClick={printIt}><Icon name="printer" size={14} /> Print</button>
        <button className="btn primary" onClick={load}><Icon name="search" size={14} /> Show</button></>}
      side={<>
        {!report && groups.map((g) => (
          <div key={g}><div className="fin-sidehead">{g}</div>
            {HR_REPORTS.filter((r) => r[2] === g).map((r) => <button key={r[0]} className={'hr-reprow' + (r[0] === key ? ' on' : '')}
              onClick={() => setKey(r[0])}>{r[1]}</button>)}</div>
        ))}
        {def[3].length > 0 && <div className="fin-sidehead">Filters</div>}
        {def[3].includes('month') && <MonthField value={p.month} onChange={set('month')} />}
        {def[3].includes('date_from') && <DateRange from={p.date_from} to={p.date_to} setFrom={set('date_from')} setTo={set('date_to')} />}
        {def[3].includes('as_on') && <DateField label="As on" value={p.as_on} onChange={set('as_on')} />}
        {def[3].includes('days') && <F label="Within (days)"><input type="number" value={p.days} onChange={set('days')} /></F>}
        {def[3].includes('status') && <F label="Status"><Sel value={p.status} onChange={set('status')} allowBlank={false}
          options={[{ value: 'current', label: 'Working' }, { value: 'exited', label: 'Left' }, { value: '', label: 'Everyone' }]} /></F>}
        {def[3].includes('department_id') && <F label="Department"><Sel value={p.department_id} onChange={set('department_id')} placeholder="All"
          options={o.departments.map((x) => ({ value: x.id, label: x.name }))} /></F>}
        {def[3].includes('location') && <F label="Location"><Sel value={p.location} onChange={set('location')} placeholder="All" options={o.locations} /></F>}
      </>}>
      {!d ? <Loading>Reading…</Loading> : d.rows.length === 0 ? <Empty>Nothing to show for these filters.</Empty> : (
        <div className="tablewrap fin-scroll"><table className="items fin-table">
          <thead><tr>{d.columns.map((c) => <th key={c} className={isNum(c) ? 'r' : ''}>{c}</th>)}</tr></thead>
          <tbody>{d.rows.map((r, i) => (
            <tr key={i}>{d.columns.map((c) => <td key={c} className={typeof r[c] === 'number' ? 'r' : ''}>
              {typeof r[c] === 'number' ? r[c].toLocaleString('en-IN', { maximumFractionDigits: 2 }) : /^\d{4}-\d{2}-\d{2}$/.test(String(r[c] || '')) ? fmtDate(r[c]) : r[c] ?? ''}</td>)}</tr>
          ))}</tbody>
          {Object.keys(d.totals || {}).length > 0 && <tfoot><tr>{d.columns.map((c, i) => <td key={c} className="r">
            {d.totals[c] != null ? d.totals[c].toLocaleString('en-IN', { maximumFractionDigits: 2 }) : i === 0 ? 'Total' : ''}</td>)}</tr></tfoot>}
        </table></div>
      )}
    </HrPage>
  )
}

// ==========================================================================
//  HR Settings
// ==========================================================================
function HrSettings({ toast }) {
  const [o, reloadOpts] = useHrOptions()
  const [tab, setTab] = useState('departments')
  return (
    <HrPage title="HR Settings" sub="The lists every HR screen picks from, and the statutory rates payroll works with">
      <Tabs tabs={[['departments', 'Departments'], ['designations', 'Designations'], ['holidays', 'Holidays'], ['leave', 'Leave types'],
        ['payroll', 'Payroll & statutory'], ['devices', 'Attendance devices']]} value={tab} onChange={setTab} />
      {tab === 'departments' && <MasterTable kind="departments" noun="department" toast={toast} onChange={reloadOpts}
        cols={[['name', 'Department'], ['code', 'Code'], ['head', 'Head'], ['employees', 'People'], ['active', 'In use']]}
        fields={masterDefs(o).departments.fields} blank={masterDefs(o).departments.blank} />}
      {tab === 'designations' && <MasterTable kind="designations" noun="designation" toast={toast} onChange={reloadOpts}
        cols={[['name', 'Designation'], ['department', 'Department'], ['grade', 'Grade'], ['level', 'Level'], ['employees', 'People'], ['active', 'In use']]}
        fields={masterDefs(o).designations.fields} blank={masterDefs(o).designations.blank} />}
      {tab === 'holidays' && <MasterTable kind="holidays" noun="holiday" toast={toast} onChange={reloadOpts}
        cols={[[(r) => fmtDate(r.date), 'Date'], ['name', 'Holiday'], [(r) => (r.optional ? 'Restricted' : 'Closed'), 'Kind']]}
        fields={[['date', 'Date', 'date', true], ['name', 'Name', 'text', true], ['optional', 'Restricted (optional) — the shop stays open', 'check']]}
        blank={{ date: '', name: '', optional: false }} />}
      {tab === 'leave' && <MasterTable kind="leave_types" noun="leave type" toast={toast} onChange={reloadOpts}
        cols={[['code', 'Code'], ['name', 'Leave'], [(r) => `${r.annual_quota} / year`, 'Quota'], ['accrual', 'Credited'],
          [(r) => r.carry_forward_max || 0, 'Carry forward'], [(r) => (r.paid ? 'Paid' : 'Unpaid'), 'Pay'], ['allow_negative', 'Beyond balance'], ['active', 'In use']]}
        fields={[['code', 'Code', 'text', true], ['name', 'Name', 'text', true], ['annual_quota', 'Days a year', 'number'],
          ['accrual', 'Credited', 'select', false, [{ value: 'monthly', label: 'Monthly (a twelfth each month)' }, { value: 'yearly', label: 'Yearly (all in January)' }, { value: 'none', label: 'By hand only' }]],
          ['carry_forward_max', 'Carry forward up to (days)', 'number'], ['paid', 'Paid leave', 'check'], ['allow_negative', 'May go beyond the balance', 'check'],
          ['color', 'Colour', 'color'], ['active', 'In use', 'check']]}
        blank={{ code: '', name: '', annual_quota: 12, accrual: 'monthly', carry_forward_max: 0, paid: true, allow_negative: false, color: '#2F8F68', active: true }} />}
      {tab === 'payroll' && <StatutorySettings toast={toast} />}
      {tab === 'devices' && <DevicesBody toast={toast} />}
    </HrPage>
  )
}

function StatutorySettings({ toast }) {
  const [s, setS] = useState(null)
  const [busy, run] = useRun(toast)
  useEffect(() => { api.hrGet('settings').then(setS).catch((e) => toast(errText(e), 'err')) }, [toast])
  if (!s) return <Loading>Reading…</Loading>
  const set = (k) => (v) => setS({ ...s, [k]: v?.target ? (v.target.type === 'checkbox' ? v.target.checked : v.target.value) : v })
  const save = async () => {
    const body = { ...s }
    delete body.device_key
    const r = await run(() => api.hrSend('PUT', 'settings', body), '✓ Settings saved — they apply the next time a month is worked out')
    if (r) setS(r)
  }
  const slabs = (k, cols) => (
    <table className="items fin-table fin-entry"><thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}<th /></tr></thead>
      <tbody>{(s[k] || []).map((row, i) => (
        <tr key={i}>{(Array.isArray(row) ? row : [row.upto, row.amount]).map((v, j) => (
          <td key={j}><input type="number" value={v ?? ''} placeholder={j === 0 ? 'above all' : ''} onChange={(e) => {
            const val = e.target.value === '' ? null : +e.target.value
            const next = (s[k] || []).map((r, x) => (x !== i ? r : Array.isArray(r) ? r.map((c, y) => (y === j ? val : c))
              : { ...r, [j === 0 ? 'upto' : 'amount']: val }))
            setS({ ...s, [k]: next })
          }} /></td>))}
          <td><button className="rowdel" onClick={() => setS({ ...s, [k]: s[k].filter((_, x) => x !== i) })}>×</button></td></tr>
      ))}</tbody></table>
  )
  const addRow = (k) => setS({ ...s, [k]: [...(s[k] || []), Array.isArray((s[k] || [])[0]) ? [null, 0] : { upto: null, amount: 0 }] })
  return (
    <div className="hr-settings">
      <div className="fin-form">
        <h5>Provident Fund</h5>
        <F label="Employee %"><input type="number" step="0.01" value={s.pf_employee_pct} onChange={set('pf_employee_pct')} /></F>
        <F label="Employer %"><input type="number" step="0.01" value={s.pf_employer_pct} onChange={set('pf_employer_pct')} /></F>
        <F label="Of which pension (EPS) %"><input type="number" step="0.01" value={s.eps_pct} onChange={set('eps_pct')} /></F>
        <F label="Wage ceiling ₹"><input type="number" value={s.pf_wage_ceiling} onChange={set('pf_wage_ceiling')} /></F>
        <F label="Cap at the ceiling"><label className="fin-check"><input type="checkbox" checked={!!s.pf_cap_at_ceiling} onChange={set('pf_cap_at_ceiling')} /> PF on wages up to the ceiling only</label></F>
        <h5>ESI</h5>
        <F label="Employee %"><input type="number" step="0.01" value={s.esi_employee_pct} onChange={set('esi_employee_pct')} /></F>
        <F label="Employer %"><input type="number" step="0.01" value={s.esi_employer_pct} onChange={set('esi_employer_pct')} /></F>
        <F label="Gross up to ₹ is covered"><input type="number" value={s.esi_wage_limit} onChange={set('esi_wage_limit')} /></F>
        <h5>Days, overtime, unmarked days</h5>
        <F label="A month's pay is divided by"><Sel value={s.month_days} onChange={set('month_days')} allowBlank={false}
          options={[{ value: 'calendar', label: 'Days in the month' }, { value: '26', label: '26 days' }, { value: '30', label: '30 days' }]} /></F>
        <F label="Overtime paid at"><input type="number" step="0.25" value={s.ot_multiplier} onChange={set('ot_multiplier')} /></F>
        <F label="…times the hourly rate of"><Sel value={s.ot_on} onChange={set('ot_on')} allowBlank={false}
          options={[{ value: 'gross', label: 'Gross salary' }, { value: 'basic', label: 'Basic + DA' }]} /></F>
        <F label="A day nobody marked counts as"><Sel value={s.unmarked_days} onChange={set('unmarked_days')} allowBlank={false}
          options={[{ value: 'present', label: 'Present (attendance not kept)' }, { value: 'absent', label: 'Absent (loss of pay)' }]} /></F>
        <F label="Employee codes start with"><input value={s.employee_prefix} onChange={set('employee_prefix')} /></F>
        <h5>Income tax (TDS estimate)</h5>
        <F label="Standard deduction — new"><input type="number" value={s.std_deduction_new} onChange={set('std_deduction_new')} /></F>
        <F label="Standard deduction — old"><input type="number" value={s.std_deduction_old} onChange={set('std_deduction_old')} /></F>
        <F label="No tax up to — new (rebate)"><input type="number" value={s.new_regime_rebate_upto} onChange={set('new_regime_rebate_upto')} /></F>
        <F label="No tax up to — old (rebate)"><input type="number" value={s.old_regime_rebate_upto} onChange={set('old_regime_rebate_upto')} /></F>
        <F label="Cess %"><input type="number" value={s.cess_pct} onChange={set('cess_pct')} /></F>
      </div>
      <div className="hr-three">
        <div><div className="fin-sidehead">Professional Tax · {s.pt_state}</div>{slabs('pt_slabs', ['Gross up to ₹', '₹ a month'])}
          <button className="btn sm" onClick={() => addRow('pt_slabs')}><Icon name="plus" size={13} /> Row</button></div>
        <div><div className="fin-sidehead">New regime slabs</div>{slabs('new_regime_slabs', ['Income up to ₹', 'Rate %'])}
          <button className="btn sm" onClick={() => addRow('new_regime_slabs')}><Icon name="plus" size={13} /> Row</button></div>
        <div><div className="fin-sidehead">Old regime slabs</div>{slabs('old_regime_slabs', ['Income up to ₹', 'Rate %'])}
          <button className="btn sm" onClick={() => addRow('old_regime_slabs')}><Icon name="plus" size={13} /> Row</button></div>
      </div>
      <div className="small muted fin-note">A blank “up to” is the last row: everything above. Changes apply to months worked out from now on —
        an approved month keeps the figures it was approved with.</div>
      <div style={{ marginTop: 12 }}><button className="btn primary" disabled={busy} onClick={save}>Save settings</button></div>
    </div>
  )
}

// ==========================================================================
//  The screens
// ==========================================================================
export const HR_SCREENS = [
  ['hr_dashboard', 'HR Dashboard', "Headcount, today's attendance, approvals and what is coming up", 'dashboard'],
  ['hr_employees', 'Employees', 'Records, contracts, documents and role history', 'users'],
  ['hr_org', 'Organisation Chart', 'Who reports to whom, and each department', 'layers'],
  ['hr_attendance', 'Attendance', 'The month register — by hand, file, device or till', 'clipcheck'],
  ['hr_shifts', 'Shifts & Roster', 'Shift timings and who works which, week by week', 'history'],
  ['hr_overtime', 'Overtime', 'Hours past the shift, approved for pay', 'trending'],
  ['hr_leave', 'Leave', 'Requests, approvals, balances and accrual', 'listchecks'],
  ['hr_advances', 'Advances & Loans', 'Given to staff, recovered from salary', 'card'],
  ['hr_incentives', 'Sales Incentive', 'Schemes on value, barcode, brand and stock age', 'sparkle'],
  ['hr_payroll', 'Payroll', 'Work out, approve and pay — PF, ESI, PT and TDS', 'rupee'],
  ['hr_payslips', 'Payslips', "Each person's slip, printable", 'receipt'],
  ['hr_performance', 'Performance', 'Goals, KPIs and appraisals', 'award'],
  ['hr_reports', 'HR Reports', 'Registers and the statutory statements', 'spreadsheet'],
  ['hr_settings', 'HR Settings', 'Departments, holidays, leave types and statutory rates', 'sliders'],
]

export default function HR({ screen, toast, go, report }) {
  switch (screen) {
    case 'hr_dashboard': return <HrDashboard toast={toast} go={go} />
    case 'hr_employees': return <Employees toast={toast} />
    case 'hr_org': return <OrgChart toast={toast} />
    case 'hr_attendance': return <Attendance toast={toast} />
    case 'hr_shifts': return <Shifts toast={toast} />
    case 'hr_overtime': return <Overtime toast={toast} />
    case 'hr_leave': return <Leave toast={toast} />
    case 'hr_advances': return <Advances toast={toast} />
    case 'hr_incentives': return <Incentives toast={toast} />
    case 'hr_payroll': return <Payroll toast={toast} />
    case 'hr_payslips': return <Payslips toast={toast} />
    case 'hr_performance': return <Performance toast={toast} />
    case 'hr_reports': return <HrReports toast={toast} report={report} />
    case 'hr_settings': return <HrSettings toast={toast} />
    default: return <Empty>Unknown HR screen.</Empty>
  }
}
