// ==========================================================================
//  The parts the HR screens (HR.jsx) are drawn from
//  ------------------------------------------------------------------------
//  HR came from ESSA, where it shares these with Finance. Here they are built
//  on WAREXX's own pieces — the page band every WAREXX screen opens with, its
//  status pills, its empty state, its dialogs — so HR reads as one more WAREXX
//  module rather than a visitor from another app.
// ==========================================================================
import React, { useEffect, useRef, useState } from 'react'
import { DateField } from './App.jsx'
import { Icon } from './ui/icons.jsx'
import { StatusBadge, EmptyState } from './ui/kit.jsx'

export const errText = (e) => e?.detail || e?.message || 'Something went wrong'

export function monthStart() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-01`
}
export function todayIso() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

// A whole table to a CSV file, from what is on screen.
export function downloadCsv(name, cols, rows) {
  const esc = (v) => {
    const s = v == null ? '' : String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const text = [cols.map((c) => esc(c[1])).join(','),
    ...rows.map((r) => cols.map((c) => esc(typeof c[0] === 'function' ? c[0](r) : r[c[0]])).join(','))].join('\n')
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' }))
  a.download = `${name}.csv`
  a.click()
  setTimeout(() => URL.revokeObjectURL(a.href), 1000)
}

// The page: WAREXX's title band, then a filter column on the left (when the
// screen has one) and the work on the right.
export function FinPage({ title, sub, actions, side, children, wideSide, crumb = 'Finance' }) {
  return (
    <div className="screen">
      <div className="pagehead">
        <h2><span className="fin-crumb">{crumb} /</span> {title}</h2>
        {sub && <div className="pagesub small">{sub}</div>}
        {actions && <div className="fin-actions">{actions}</div>}
      </div>
      <div className="fin-wrap">
        {side && <aside className={'fin-side' + (wideSide ? ' wide' : '')}>{side}</aside>}
        <div className="fin-main">{children}</div>
      </div>
    </div>
  )
}

export function F({ label, children, req, hint, style }) {
  return (
    <div className="field" style={style}>
      <label>{label}{req && <span className="req"> *</span>}</label>
      {children}
      {hint && <div className="small fin-hint">{hint}</div>}
    </div>
  )
}

// A select whose options are strings or {value,label}; blank = "any".
export function Sel({ value, onChange, options, placeholder = 'Any', allowBlank = true, disabled }) {
  return (
    <select value={value ?? ''} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
      {allowBlank && <option value="">{placeholder}</option>}
      {(options || []).map((o) => {
        const v = typeof o === 'object' ? o.value ?? o.id ?? o.name : o
        const l = typeof o === 'object' ? o.label ?? o.name : o
        return <option key={v} value={v}>{l}</option>
      })}
    </select>
  )
}

// A header box that ticks all, then one box per item.
export function CheckList({ title, items, value, onChange, height = 132 }) {
  const all = items.length > 0 && items.every((i) => value.includes(i.value ?? i))
  const toggle = (v) => onChange(value.includes(v) ? value.filter((x) => x !== v) : [...value, v])
  return (
    <div className="chklist">
      <label className="chkhead">
        <span>{title}</span>
        <input type="checkbox" checked={all}
          onChange={() => onChange(all ? [] : items.map((i) => i.value ?? i))} />
      </label>
      <div className="chkbody" style={{ maxHeight: height }}>
        {items.length === 0 && <div className="small muted" style={{ padding: '6px 10px' }}>Nothing to choose yet</div>}
        {items.map((i) => {
          const v = i.value ?? i
          return (
            <label key={v} className="chkrow">
              <span>{i.label ?? i}</span>
              <input type="checkbox" checked={value.includes(v)} onChange={() => toggle(v)} />
            </label>
          )
        })}
      </div>
    </div>
  )
}

export function DateRange({ from, to, setFrom, setTo }) {
  return (
    <div className="fin-range">
      <DateField label="From" value={from} onChange={setFrom} />
      <DateField label="To" value={to} onChange={setTo} />
    </div>
  )
}

// WAREXX's empty state, compact, as every other WAREXX list shows "nothing here".
export function Empty({ children }) {
  return <EmptyState compact icon="inbox">{children}</EmptyState>
}

// Dialogs open on top of dialogs; Escape closes only the top one.
const openModals = []
export function Modal({ title, onClose, children, foot, wide }) {
  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    const me = {}
    openModals.push(me)
    const esc = (e) => { if (e.key === 'Escape' && openModals[openModals.length - 1] === me) close.current() }
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('keydown', esc); openModals.splice(openModals.indexOf(me), 1) }
  }, [])
  return (
    <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className={'modal fin-modal' + (wide ? ' wide' : '')} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head"><b>{title}</b>
          <button className="modal-x" onClick={onClose} aria-label="Close"><Icon name="x" size={16} /></button></div>
        <div className="modal-body">{children}</div>
        {foot && <div className="modal-foot">{foot}</div>}
      </div>
    </div>
  )
}

// ESSA's status words, worn as WAREXX's status pills.
const TONE = { confirmed: 'ok', posted: 'ok', paid: 'ok', pending: 'warn', cancelled: 'err', draft: 'muted', info: 'info' }
export function Badge({ tone = 'draft', children, title }) {
  return <StatusBadge tone={TONE[tone] || 'muted'} title={title}>{children}</StatusBadge>
}

// load-on-demand with a busy flag and the server's sentence on failure
export function useRun(toast) {
  const [busy, setBusy] = useState(false)
  const run = async (fn, okMsg) => {
    setBusy(true)
    try {
      const r = await fn()
      if (okMsg) toast(typeof okMsg === 'function' ? okMsg(r) : okMsg, 'ok')
      return r
    } catch (e) {
      toast(errText(e), 'err')
      return null
    } finally { setBusy(false) }
  }
  return [busy, run]
}
