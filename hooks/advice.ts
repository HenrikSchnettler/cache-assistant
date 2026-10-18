// What the band says: formatting and the advice for each cache situation.
// Pure functions of the window, the clock and the threshold.

import type { CacheView, Gate, Notice } from '../types'

export type Advice = {
  text: string
  /** `warn`: acting first saves a large re-write; `calm`: nothing to do. */
  tone: 'warn' | 'calm'
  /** What the band offers as buttons, in order: the cures for a cold, large cache. */
  actions?: readonly Action[]
}

export type Action = 'compact' | 'clear' | 'send'

export const fmtTokens = (n: number | null): string =>
  n === null ? '?' : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)

// Minutes while there is plenty left, m:ss in the last five.
export const fmtLeft = (ms: number): string => {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  if (seconds >= 300) return `${Math.ceil(seconds / 60)}m`

  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

export const shortModel = (id: string): string =>
  id.replace(/^claude-/, '').replace(/-\d{8}$/, '')

export const remainingMs = (view: CacheView, now: number): number =>
  view.ttlMs === null ? 0 : view.anchorMs + view.ttlMs - now

// The countdown as drawn: the band redraws only when this changes.
export const clockLabel = (view: CacheView | null, now: number): string => {
  if (view === null || view.ttlMs === null) return ''
  const left = remainingMs(view, now)

  return left <= 0 ? 'expired' : fmtLeft(left)
}

// A resumed or forked session before its first turn has no transcript of its
// own to read; SessionStart says how long ago the last response was and how
// much the first request re-sends. The tier is inferred: gone cold within the
// hour means 5m, still warm past five minutes means 1h.
export const seedFromResume = (
  start: { seconds_since_last_response?: number; context_tokens?: number; prompt_cache_likely_expired?: boolean },
  now: number,
): CacheView | null => {
  const idle = start.seconds_since_last_response
  if (idle === undefined || !start.context_tokens) return null
  const isCold = start.prompt_cache_likely_expired === true
  const tier = (isCold ? idle < 3600 : idle < 300) ? '5m' : '1h'

  return {
    tier,
    anchorMs: now - idle * 1000,
    ttlMs: tier === '5m' ? 300_000 : 3_600_000,
    tokens: start.context_tokens,
    pingMs: tier === '5m' ? 240_000 : 1_800_000,
    miss: null,
  }
}

// A keep-alive ping is due one interval after the cache was last touched.
export const isDue = (view: CacheView, now: number): boolean =>
  view.pingMs !== null && now - view.anchorMs >= view.pingMs

export const isLarge = (tokens: number | null, threshold: number): boolean =>
  tokens === null || tokens >= threshold

type AdviceInput = {
  view: CacheView | null
  now: number
  threshold: number
  note: Notice | null
  isKeepAliveOn: boolean
  gate: Gate
}

// The guard blocks a large cold re-write until Allow send anyway is pressed; once it
// is, the button gives way to saying the next message goes through.
export const advise = (input: AdviceInput): Advice | null => {
  const advice = adviseCache(input)
  if (!input.gate.isArmed) return advice
  const armed = 'Sending anyway is allowed: your next message goes through.'
  if (advice === null) return { tone: 'calm', text: armed }
  const actions = advice.actions?.filter(a => a !== 'send')

  return { ...advice, text: `${advice.text} ${armed}`, actions: actions?.length ? actions : undefined }
}

const adviseCache = (input: AdviceInput): Advice | null => {
  const { view, now, threshold, note, isKeepAliveOn, gate } = input

  if (note?.kind === 'switch') {
    const size = `~${fmtTokens(note.tokens)} tokens`
    const route = `${shortModel(note.from)} → ${shortModel(note.to)}`
    if (note.warmUntilMs === null || note.warmUntilMs <= now) {
      // The switch costs nothing extra; what the cold cache calls for still stands.
      const cold = adviseCache({ ...input, note: null })
      if (cold?.tone === 'warn') return cold
      return { tone: 'calm', text: `Model switched ${route}. The old cache was already cold, so nothing extra is lost.` }
    }
    if (!isLarge(note.tokens, threshold)) {
      return { tone: 'calm', text: `Model switched ${route}: only ${size} to re-cache, just keep going.` }
    }

    return {
      tone: 'warn',
      actions: ['send'],
      text:
        `Model switched ${route}: the next message re-caches ${size} from cold. ` +
        `Switch back within ${fmtLeft(note.warmUntilMs - now)} and the ${shortModel(note.from)} cache is untouched.`,
    }
  }

  if (note?.kind === 'miss') {
    const cause =
      note.causes.length > 0
        ? `after you changed ${note.causes.join(', ')}`
        : 'so something in the prefix changed (system prompt, tools, an MCP server, effort)'

    return {
      tone: 'calm',
      text:
        `The last request missed the warm cache (read ${fmtTokens(note.read)} of ~${fmtTokens(note.expected)} tokens), ${cause}. ` +
        'It is re-cached now; nothing to do.',
    }
  }

  if (view === null || view.ttlMs === null) return null
  const left = remainingMs(view, now)
  const size = `~${fmtTokens(view.tokens)} tokens`
  const large = isLarge(view.tokens, threshold)

  if (left <= 0) {
    if (!large) {
      return { tone: 'calm', text: `Only ${size} to re-cache (under your ${fmtTokens(threshold)} threshold): just keep going.` }
    }

    return {
      tone: 'warn',
      actions: ['compact', 'clear', 'send'],
      text:
        `Sending now re-caches ${size} from cold. Same topic: compact first and carry on with a small context. ` +
        'New topic: clear.',
    }
  }

  // A model switch the guard caught (`model_effort`: a block left by 0.4.0).
  if (gate.blocked === 'model_effort' || gate.blocked === 'model_switch') {
    return {
      tone: 'warn',
      actions: ['send'],
      text: `You changed the ${gate.blocked === 'model_switch' ? 'model' : 'model or effort'}, which re-caches ${size} from cold. Switch back and the cache is untouched.`,
    }
  }

  if (large && !isKeepAliveOn && left <= Math.max(60_000, view.ttlMs * 0.2)) {
    return {
      tone: 'warn',
      text: `${size} go cold in ${fmtLeft(left)}. Send your next message before then, or turn keep-alive on if you are stepping away.`,
    }
  }

  return null
}
