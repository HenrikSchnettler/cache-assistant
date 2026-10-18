/** The cache window as lib/cache_core.py derives it from the transcript. */
export type CacheView = {
  tier: '5m' | '1h' | null
  /** When the cached prefix was last read or written, ms since the epoch. */
  anchorMs: number
  ttlMs: number | null
  /** Tokens a cold re-write of the current prefix costs. */
  tokens: number | null
  /** The keep-alive interval of this tier. */
  pingMs: number | null
  miss: CacheMiss | null
}

/** A request inside the open window that did not read the cached prefix. */
export type CacheMiss = {
  atMs: number
  expected: number
  read: number
  isModelChange: boolean
}

export type KeepAlive = { isOn: boolean; pings: number }

export type Notice =
  | {
      kind: 'switch'
      from: string
      to: string
      tokens: number
      /** When the cache of `from` lapses; null when it was already cold. */
      warmUntilMs: number | null
    }
  | { kind: 'miss'; expected: number; read: number; causes: readonly string[] }

/** The guard as the band sees it: a block waiting for Allow send anyway, or an ack. */
export type Gate = {
  /** Allow send anyway was pressed: the guard lets the next message through. */
  isArmed: boolean
  /** Why the guard blocked the last send (`expiry`, `model_switch`; `model_effort` from 0.4.0). */
  blocked: string | null
}

/** What the session is busy with, drawn in place of the band's controls. */
export type Busy = 'working' | 'compacting' | 'clearing'

export type SessionRef = { id: string; transcript: string | null }

declare module 'claude-code' {
  interface PluginState {
    'cache-assistant': {
      cache: CacheView | null
      clock: string
      keepAlive: KeepAlive
      notice: Notice | null
      isHidden: boolean
      busy: Busy | null
      gate: Gate
      frame: number
      session: SessionRef | null
    }
  }
}
