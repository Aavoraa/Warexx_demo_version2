// Loading, the WAREXX way: the skeleton state for a panel, a small ring for
// a button or a line of text. (ESSA's HR screens call these two by name.)
import React from 'react'
import { LoadingState } from './ui/kit.jsx'

export function Hourglass({ size = 16, label = 'Loading' }) {
  return <span className="hr-spin" style={{ width: size, height: size }} role="status" aria-label={label} />
}

export function Loading({ children = 'Loading…', inline, style }) {
  if (inline) {
    return (
      <span className="hr-spin-inline" style={style}>
        <Hourglass size={14} label={typeof children === 'string' ? children : 'Loading'} />
        {children && <span>{children}</span>}
      </span>
    )
  }
  return <div style={style}><LoadingState compact rows={3} label={typeof children === 'string' ? children : 'Loading…'} /></div>
}
