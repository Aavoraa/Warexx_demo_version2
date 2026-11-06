// Imported first by main.jsx. The screens call /api/...; on this page those calls
// go to the live backend at /live/api/..., which the demo's service worker (if this
// browser has one) leaves alone. Only the session cookie identifies the person: any
// header api.js added from browser storage (the demo keeps its own "demo" token
// there, on this same origin) is dropped.
const real = window.fetch.bind(window)

window.fetch = (url, opts = {}) => {
  if (typeof url === 'string' && url.startsWith('/api/')) {
    const headers = new Headers(opts.headers || {})
    headers.delete('Authorization')
    headers.delete('X-Essa-Token')
    headers.delete('X-Essa-Warehouse')
    return real('/live' + url, { ...opts, headers, credentials: 'same-origin' })
  }
  return real(url, opts)
}
