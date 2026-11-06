// WAREXX Live: HR, Finance and the CRM on the real backend (live-api/), for staff
// with an account. It reuses the WAREXX sign-in, sidebar and screens. Unlike the
// demo it starts no service worker and nothing is recorded: every read and save
// goes to the server through /live/api and /live/pos.
import './live-fetch.js'
import React, { useEffect, useMemo, useState } from 'react'
import ReactDOM from 'react-dom/client'
import '../vendor/warexx-app/src/styles.css'
import '../vendor/warexx-app/src/design-system.css'
import '../vendor/warexx-app/src/ui/warexx.css'
import '../vendor/warexx-app/src/ui/screens.css'
import { api, session, setUnauthorizedHandler } from '../vendor/warexx-app/src/api.js'
import { CRM_KEYS, Icon, LoginScreen, NavSidebar, POS_SCREENS, Users } from '../vendor/warexx-app/src/App.jsx'
import HR, { HR_SCREENS } from '../vendor/warexx-app/src/HR.jsx'
import Finance, { FIN_SCREENS } from '../vendor/warexx-app/src/Finance.jsx'
import { F, Modal, errText } from '../vendor/warexx-app/src/hr-kit.jsx'
import { Crumbs, PageCtx } from '../vendor/warexx-app/src/ui/kit.jsx'

const TAB_KEY = 'wx_live_tab'
const CRM = POS_SCREENS.filter((p) => CRM_KEYS.has(p.key))
const atLeastAdmin = (role) => ['admin', 'superadmin', 'superboss'].includes(role)
const atLeastSuper = (role) => ['superadmin', 'superboss'].includes(role)

function ChangePassword({ toast, onClose }) {
  const [cur, setCur] = useState('')
  const [next, setNext] = useState('')
  const [busy, setBusy] = useState(false)
  const save = async () => {
    setBusy(true)
    try {
      await api.changePassword(cur, next)
      toast('Password changed', 'ok')
      onClose()
    } catch (e) { toast(errText(e), 'err') } finally { setBusy(false) }
  }
  return (
    <Modal title="Change password" onClose={onClose}
      foot={<><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || !cur || !next} onClick={save}>Save password</button></>}>
      <F label="Current password"><input type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} /></F>
      <F label="New password"><input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} /></F>
    </Modal>
  )
}

function Live() {
  const [checked, setChecked] = useState(false)
  const [me, setMe] = useState(null)
  const [tab, setTabState] = useState(() => {
    try { return localStorage.getItem(TAB_KEY) || 'hr_dashboard' } catch { return 'hr_dashboard' }
  })
  const [navOpen, setNavOpen] = useState(false)
  const [pw, setPw] = useState(false)
  const [msg, setMsg] = useState(null)

  const signedOut = () => { session.clear(); setMe(null) }
  useEffect(() => {
    setUnauthorizedHandler(signedOut)
    // the session is the HttpOnly cookie; this asks the server whose it is
    api.verifyToken('').then((r) => { if (r.ok) setMe(r) }).catch(() => {}).finally(() => setChecked(true))
  }, [])
  useEffect(() => {
    if (!msg) return undefined
    const t = setTimeout(() => setMsg(null), 4000)
    return () => clearTimeout(t)
  }, [msg])

  const toast = useMemo(() => (m, kind) => setMsg({ m, kind }), [])
  const role = me?.role || ''
  // An empty grant map means unrestricted; the server enforces all of this anyway.
  const granted = me?.permissions?.screens || null
  const has = (perm) => !granted || !!(granted[perm] || []).length

  const items = useMemo(() => {
    if (!me) return []
    const mod = ([key, label, blurb, icon]) => ({ key, label, blurb, icon })
    return [
      ...(atLeastAdmin(role) && has('hr') ? HR_SCREENS.map(mod) : []),
      ...(atLeastAdmin(role) && has('finance') ? FIN_SCREENS.map(mod) : []),
      ...CRM,
      ...(atLeastSuper(role) ? [{ key: 'users', label: 'Users & Access', icon: 'users', blurb: 'Who can sign in, and how much of this they see' }] : []),
    ]
  }, [me, role]) // eslint-disable-line react-hooks/exhaustive-deps

  const setTab = (k) => {
    setTabState(k)
    try { localStorage.setItem(TAB_KEY, k) } catch { /* private mode */ }
  }
  const current = items.find((m) => m.key === tab) || items[0]
  const logout = () => { api.logout(); signedOut() }

  if (!checked) return <div className="login-wrap"><div className="login-bg" /></div>
  if (!me) {
    return <LoginScreen onLogin={(_t, user, r, permissions, full_name) => setMe({ ok: true, user, role: r, permissions, full_name })} />
  }

  const k = current?.key
  const crm = CRM.find((p) => p.key === k)
  const navProps = { ws: 'live', items, tab: k, go: setTab,
    foot: <><span className="wxnav-role">{me.full_name || me.user}</span><span className="wxnav-ver">WAREXX Live</span></> }
  return (
    <div className="app">
      <div className="shell">
        <NavSidebar {...navProps} />
        <div className="shellcol">
          <header className="topbar wx-top">
            <button className="th-icon th-menu" onClick={() => setNavOpen(true)} aria-label="Open navigation menu" title="Menu"><Icon name="menu" size={20} /></button>
            <Crumbs items={['WAREXX Live', current?.label]} className="wx-top-crumbs" />
            <div className="spacer" />
            <button className="btn sm" onClick={() => setPw(true)}>Change password</button>
            <button className="btn sm" onClick={logout}>Sign out</button>
          </header>
          <PageCtx.Provider value={{ crumbs: [], group: 'WAREXX Live', icon: null }}>
            <main className="shellmain">
              {crm ? (
                <div className="posframe"><iframe key={crm.path} src={'/live/pos' + crm.path} title={crm.label} /></div>
              ) : String(k).startsWith('hr_') ? (
                <HR key={k} screen={k} toast={toast} go={setTab} />
              ) : String(k).startsWith('fin_') ? (
                <Finance key={k} screen={k} toast={toast} />
              ) : k === 'users' ? (
                <Users toast={toast} me={me.user} />
              ) : null}
            </main>
          </PageCtx.Provider>
        </div>
      </div>
      {navOpen && <NavSidebar drawer {...navProps} onClose={() => setNavOpen(false)} />}
      {pw && <ChangePassword toast={toast} onClose={() => setPw(false)} />}
      {msg && <div className={'toast ' + (msg.kind || '')} role="status">{String(msg.m)}</div>}
    </div>
  )
}

ReactDOM.createRoot(document.getElementById('root')).render(<Live />)
