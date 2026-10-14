// Cache Assistant — the band above the prompt.
//
// Shows what the status line shows (tier, countdown, cold re-write size) plus
// advice on what to do next and a keep-alive switch. The cache window itself
// still comes from lib/cache_core.py (via statusline/cache_status.py --json),
// read once per turn and on the events that move it; the countdown between
// reads is plain arithmetic on the anchor, so nothing runs per second but a
// string compare.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, Timer } from 'claude-code'

import type { Busy, CacheView, Gate, KeepAlive, Notice, SessionRef } from '../types'
import type { Action } from './advice'
import {
  advise,
  clockLabel,
  fmtLeft,
  fmtTokens,
  isDue,
  remainingMs,
  seedFromResume,
  shortModel,
} from './advice'

const cache = atom({ plugin: 'cache-assistant', key: 'cache' } as const, null)
const clock = atom({ plugin: 'cache-assistant', key: 'clock' } as const, '')
const keepAlive = atom({ plugin: 'cache-assistant', key: 'keepAlive' } as const, {
  isOn: false,
  pings: 0,
})
const notice = atom({ plugin: 'cache-assistant', key: 'notice' } as const, null)
const isHidden = atom({ plugin: 'cache-assistant', key: 'isHidden' } as const, false)
const session = atom({ plugin: 'cache-assistant', key: 'session' } as const, null)
const busy = atom({ plugin: 'cache-assistant', key: 'busy' } as const, null)
const gate = atom({ plugin: 'cache-assistant', key: 'gate' } as const, { isArmed: false, blocked: null })
const frame = atom({ plugin: 'cache-assistant', key: 'frame' } as const, 0)

const SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
const BUSY_LABEL: Record<Busy, string> = {
  working: 'Claude is working…',
  compacting: 'Compacting the conversation…',
  clearing: 'Clearing the conversation…',
}

const ACTION_LABEL: Record<Action, string> = { compact: 'Compact', clear: 'Clear', send: 'Allow send anyway' }

const PING = 'Cache keep-alive ping. Reply with the single word "pong" and nothing else.'

type StatusJson = {
  have_data?: boolean
  tier?: '5m' | '1h' | null
  ttl_seconds?: number | null
  anchor_epoch?: number | null
  rewrite_tokens?: number | null
  armed?: boolean
  blocked?: string | null
  ping_interval_seconds?: number | null
  transcript?: string | null
  miss?: {
    epoch: number
    expected_tokens: number
    read_tokens: number
    model_changed: boolean
  } | null
}

let threshold = 50000
let maxPings = 12

let isWorking = false
// Steps the busy animation; runs only while something is busy.
let spinner: Timer | null = null
let isPinging = false
// The newest miss already reported (or older than this load).
let missSeenMs = 0
// Settings changed since the last turn: the suspects for a cache miss.
let changed: string[] = []

// What the session is busy with: the band draws an animated placeholder in
// place of its controls, so nothing can be pressed that would collide.
const setBusy = async ($: EngineInterface, next: Busy | null) => {
  if ((await read($, busy)) === next) return
  await update($, busy, () => next)
  if (next === null) {
    spinner?.cancel()
    spinner = null
  } else if (spinner === null) {
    spinner = $.clock.every(120, () => void update($, frame, (n: number) => (n + 1) % SPINNER.length))
  }
}

// Reads the window from the transcript through the plugin's own engine.
// `flags` are cache_status.py's writes made first: `--touch` records a
// keep-alive hit that left no transcript line, `--allow-next` the band's Send
// anyway, `--band on|off` whether that button is drawn.
const refresh = async ($: EngineInterface, ...flags: string[]): Promise<CacheView | null> => {
  const id = await $.session.id()
  const known = await read($, session)
  const transcript = known?.id === id ? known.transcript : null
  const argv = ['python3', `${$.plugin.root}/statusline/cache_status.py`, '--json', '--session', id]
  if (transcript) argv.push('--transcript', transcript)
  argv.push(...flags)

  let status: StatusJson
  try {
    const ran = await $.process.run(argv, { timeoutMs: 10_000 })
    status = JSON.parse(ran.stdout) as StatusJson
  } catch {
    return read($, cache)
  }

  const ref: SessionRef = { id, transcript: status.transcript ?? null }
  if (known?.id !== ref.id || known.transcript !== ref.transcript) {
    await update($, session, () => ref)
  }

  const view: CacheView | null =
    status.have_data && typeof status.anchor_epoch === 'number'
      ? {
          tier: status.tier ?? null,
          anchorMs: status.anchor_epoch * 1000,
          ttlMs: status.ttl_seconds ? status.ttl_seconds * 1000 : null,
          tokens: status.rewrite_tokens ?? null,
          pingMs: status.ping_interval_seconds ? status.ping_interval_seconds * 1000 : null,
          miss: status.miss
            ? {
                atMs: status.miss.epoch * 1000,
                expected: status.miss.expected_tokens,
                read: status.miss.read_tokens,
                isModelChange: status.miss.model_changed,
              }
            : null,
        }
      : null
  await update($, cache, () => view)
  await update($, clock, () => '')
  const known_gate = await read($, gate)
  const next_gate: Gate = { isArmed: status.armed === true, blocked: status.blocked ?? null }
  if (known_gate.isArmed !== next_gate.isArmed || known_gate.blocked !== next_gate.blocked) {
    await update($, gate, () => next_gate)
  }

  return view
}

const stopKeepAlive = async ($: EngineInterface, why?: string) => {
  await update($, keepAlive, (k: KeepAlive) => ({ ...k, isOn: false }))
  if (why) $.ui.toast(`Cache keep-alive off: ${why}`)
}

// One keep-alive ping: the main thread's last request again with one line
// after it, so the API reads the cached prefix and its window slides forward.
// Nothing is added to the conversation.
const ping = async ($: EngineInterface) => {
  if (isPinging || isWorking) return
  isPinging = true
  try {
    const reply = await $.model.fork({ prompt: PING })
    const readTokens = 'usage' in reply ? reply.usage.cache_read_input_tokens : 0
    if (readTokens <= 0) {
      await stopKeepAlive($, reply.isAnswered ? 'the cache was already cold' : reply.reason)
      return
    }
    await refresh($, '--touch')
    const used = (await read($, keepAlive)).pings + 1
    await update($, keepAlive, (k: KeepAlive) => ({ ...k, pings: used }))
    if (used >= maxPings) await stopKeepAlive($, `${used} pings sent without a message from you`)
  } finally {
    isPinging = false
  }
}

const toggleKeepAlive = async ($: EngineInterface): Promise<string> => {
  const now = await $.clock.now()
  const view = await read($, cache)
  if ((await read($, keepAlive)).isOn) {
    await stopKeepAlive($)
    return 'Cache keep-alive is off.'
  }
  if (!view || view.pingMs === null) return 'No cache window yet: send a message first.'
  if (remainingMs(view, now) <= 0) return 'The cache has already expired: nothing to keep alive.'
  await update($, keepAlive, () => ({ isOn: true, pings: 0 }))
  return `Cache keep-alive is on: a ping every ${fmtLeft(view.pingMs)}, at most ${maxPings}.`
}

// The band's cures for a cold, large cache: /compact and /clear as if typed,
// queued until the session is idle (a direct compaction is refused while the
// engine still counts a turn, as after an interrupted one). Our own hooks do
// not see what this module runs, so the window is re-read here.
const cure = async ($: EngineInterface, action: Action) => {
  if ((await read($, busy)) !== null) return
  if (action === 'send') {
    await refresh($, '--allow-next')
    return
  }
  await setBusy($, action === 'compact' ? 'compacting' : 'clearing')
  try {
    await $.command.run({ command: action === 'compact' ? 'compact' : 'clear' })
  } catch (err) {
    $.ui.toast(`Could not ${action}: ${err instanceof Error ? err.message : String(err)}`)
    return
  } finally {
    await setBusy($, isWorking ? 'working' : null)
  }
  if (action === 'clear') await update($, keepAlive, () => ({ isOn: false, pings: 0 }))
  await update($, notice, () => null)
  await refresh($)
}

const tick = async ($: EngineInterface) => {
  const view = await read($, cache)
  const now = await $.clock.now()
  const label = clockLabel(view, now)
  if (label !== (await read($, clock))) await update($, clock, () => label)

  if (!view || !(await read($, keepAlive)).isOn) return
  if (remainingMs(view, now) <= 0) await stopKeepAlive($, 'the cache expired')
  else if (isDue(view, now)) await ping($)
}

// After a main-loop turn: re-read the window and report a miss it shows.
const afterTurn = async ($: EngineInterface) => {
  const view = await refresh($)
  const miss = view?.miss
  if (miss && miss.atMs > missSeenMs) {
    missSeenMs = miss.atMs
    const causes = changed
    changed = []
    // A model switch is a re-write the person chose; its notice said so.
    if (!miss.isModelChange) {
      const missed: Notice = { kind: 'miss', expected: miss.expected, read: miss.read, causes }
      await update($, notice, () => missed)
      return
    }
  }
  changed = []
  await update($, notice, () => null)
}

// What the slot holds when nothing beneath draws: an empty Box (or nothing).
const isEmptyBox = (tree: unknown): boolean => {
  if (tree === null || tree === undefined) return true
  if (typeof tree !== 'object') return false
  const node = tree as { type?: string; children?: unknown }
  const children = Array.isArray(node.children) ? node.children : node.children === undefined ? [] : [node.children]

  return node.type === 'Box' && children.every(c => c === null || c === undefined || c === false || c === '')
}

export const register: Register = (on, options) => {
  threshold = Number(options.block_threshold_tokens ?? 50000)
  maxPings = Number(options.keepalive_max_pings ?? 12)

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'cache-keepalive',
      description: 'Toggle the Cache Assistant keep-alive (pings that hold the prompt cache warm)',
    })
    await $.command.register({
      name: 'cache-band',
      description: 'Show or hide the Cache Assistant band above the prompt',
    })
    // A reload drops the animation's timer: start from idle.
    await update($, busy, () => null)
    const view = await refresh($, '--band', (await read($, isHidden)) ? 'off' : 'on')
    missSeenMs = view?.miss?.atMs ?? 0
    $.clock.every(1000, () => void tick($))

    return next(e)
  })

  // A resumed or forked session: until its own transcript has a turn, show
  // what SessionStart knows about the window it inherits.
  on('classic.SessionStart', async ($, e, next) => {
    const answer = await next(e)
    if ((e.source === 'resume' || e.source === 'fork') && (await refresh($)) === null) {
      const seeded = seedFromResume(e, await $.clock.now())
      if (seeded !== null) await update($, cache, () => seeded)
    }

    return answer
  })

  on('command.run', { command: 'cache-keepalive' }, async $ => ({ text: await toggleKeepAlive($) }))

  on('command.run', { command: 'cache-band' }, async $ => {
    const hidden = !(await read($, isHidden))
    await update($, isHidden, () => hidden)
    // Hidden, the band offers no Allow send anyway: the guard takes a re-send instead.
    await refresh($, '--band', hidden ? 'off' : 'on')

    return { text: hidden ? 'Cache Assistant band hidden.' : 'Cache Assistant band shown.' }
  })

  // A send the guard blocked starts no turn: read the block for the band.
  on('prompt.submit', async ($, e, next) => {
    const submitted = await next(e)
    await refresh($)

    return submitted
  })

  on('turn.start', async ($, e, next) => {
    isWorking = true
    if ((await read($, busy)) === null) await setBusy($, 'working')
    // A message from the person: the keep-alive budget starts over.
    await update($, keepAlive, (k: KeepAlive) => (k.pings === 0 ? k : { ...k, pings: 0 }))

    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId === undefined) {
      isWorking = false
      if ((await read($, busy)) === 'working') await setBusy($, null)
      await afterTurn($)
    }

    return done
  })

  // The transcript's last line may land after turn.complete: read once more.
  on('classic.Stop', async ($, e, next) => {
    const answer = await next(e)
    await afterTurn($)

    return answer
  })

  // /compact typed, the engine's auto-compact, or the band's own button.
  on('session.compact', async ($, e, next) => {
    if (e.agentId !== undefined || e.trigger === 'precompute') return next(e)
    const before = await read($, busy)
    await setBusy($, 'compacting')
    try {
      const compacted = await next(e)
      await refresh($)
      return compacted
    } finally {
      // An auto-compact runs inside a turn: hand the band back to it.
      await setBusy($, before === 'compacting' ? 'compacting' : isWorking ? 'working' : null)
    }
  })

  on('session.end', async ($, e, next) => {
    if (e.reason === 'clear') {
      await update($, cache, () => null)
      await update($, notice, () => null)
      await update($, keepAlive, () => ({ isOn: false, pings: 0 }))
    }

    return next(e)
  })

  on('classic.PostModelSwitch', async ($, e, next) => {
    const answer = await next(e)
    const earlier = await read($, notice)
    const origin = earlier?.kind === 'switch' ? earlier : null
    if (e.source === 'resume' || origin?.from === e.to_model) {
      // Back on the model the cache was built for: nothing is lost.
      await update($, notice, () => null)
    } else {
      const view = await read($, cache)
      const now = await $.clock.now()
      const lapses = view && view.ttlMs !== null ? view.anchorMs + view.ttlMs : null
      const switched: Notice = origin
        ? { ...origin, to: e.to_model }
        : {
            kind: 'switch',
            from: e.from_model,
            to: e.to_model,
            tokens: e.context_tokens,
            warmUntilMs: e.prompt_cache_warm && lapses !== null && lapses > now ? lapses : null,
          }
      await update($, notice, () => switched)
    }

    return answer
  })

  on('config.set', async ($, e, next) => {
    const set = await next(e)
    if (set.deny === undefined && !e.key.startsWith('cache-assistant.')) changed.push(e.key)

    return set
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || e.props.view.agentId !== undefined || (await read($, isHidden))) {
      return next(e)
    }

    const view = await read($, cache)
    // Read for the redraw alone: the label changes when the countdown does.
    await read($, clock)
    const alive = await read($, keepAlive)
    const note = await read($, notice)
    const now = await $.clock.now()
    const model = shortModel(await $.session.model())
    const { Box, Button, Text } = $.ui.resolve(e)

    // A turn already running when this module loaded has set no state yet.
    const doing = (await read($, busy)) ?? (e.props.isWorking ? 'working' : null)
    const step = await read($, frame)

    const isKnown = view !== null && view.ttlMs !== null
    const left = view ? remainingMs(view, now) : 0
    // A running request reads and re-writes the prefix: no countdown applies.
    const isInUse = doing !== null
    const isExpired = isKnown && !isInUse && left <= 0
    const isClosing = isKnown && !isInUse && !isExpired && left <= Math.max(30_000, (view?.ttlMs ?? 0) * 0.1)
    const color = isInUse ? 'cyan' : !isKnown ? undefined : isExpired ? 'red' : isClosing ? 'yellow' : 'green'
    // Nothing cached yet (a new session, after /clear or a compaction): the same
    // row as every other state, an empty dot and what fills it.
    const isEmpty = view === null
    const title = view?.tier ? `cache ${view.tier}` : 'cache'
    const state = isInUse
      ? 'in use'
      : isEmpty
        ? 'empty'
        : !isKnown
          ? 'tier unknown'
          : isExpired
            ? 'expired'
            : `${fmtLeft(left)} left`
    const size = isEmpty
      ? isInUse
        ? null
        : 'fills with your next message'
      : !isKnown || isInUse
        ? null
        : `~${fmtTokens(view.tokens)} ${isExpired ? 'to re-cache' : 'cached'}`
    const advice = isInUse
      ? null
      : advise({ view, now, threshold, note, isKeepAliveOn: alive.isOn, gate: await read($, gate) })
    // Keep-alive can only hold a window that is still open, and is left alone
    // while the session is busy (a running turn keeps the cache warm itself).
    const canKeepAlive = !isInUse && (alive.isOn || (isKnown && !isExpired))
    const keepAliveLabel = alive.isOn ? `keep-alive on ${alive.pings}/${maxPings}` : 'keep-alive off'
    // A desktop draws in proportional type, where terminal spacing reads as
    // cramped; half a cell there evens its own band padding out to all sides.
    const isRoomy = e.surface !== 'terminal'
    const space = isRoomy ? 0.5 : 0
    const gap = isRoomy ? 1.5 : 1
    const indent = isRoomy ? 2.5 : 2

    // The band is one slot that every plugin's hook draws into in turn: ours
    // goes on top of whatever the plugins beneath (and the engine) draw, so
    // another plugin's band stays visible.
    const below = await next(e)
    const ours = (
      // The desktop band is narrower than the terminal's columns: let it fill
      // its own width, so the right-hand group never wraps under the left.
      <Box flexDirection="column" width={isRoomy ? undefined : e.props.bodyColumns} rowGap={space} paddingY={space}>
        <Box justifyContent="space-between" columnGap={gap * 2}>
          <Box columnGap={gap} flexShrink={1}>
            <Text color={color} dimColor={color === undefined}>
              {isEmpty ? '○' : '●'}
            </Text>
            <Text bold>{title}</Text>
            <Text color={color} dimColor={color === undefined} bold={isExpired}>
              {state}
            </Text>
            {size !== null && <Text dimColor>{size}</Text>}
          </Box>
          <Box columnGap={gap} flexShrink={0}>
            <Text dimColor>{model}</Text>
            {canKeepAlive ? (
              <Button
                key="keepalive"
                label={keepAliveLabel}
                variant={alive.isOn ? 'primary' : 'secondary'}
                onPress={async () => {
                  const said = await toggleKeepAlive($)
                  if (!(await read($, keepAlive)).isOn && !said.endsWith('is off.')) $.ui.toast(said)
                }}
              />
            ) : (
              <Text dimColor>{keepAliveLabel}</Text>
            )}
          </Box>
        </Box>
        {/* Leading: the spinner stands in the dot's column, its label under "cache". */}
        {doing !== null && (
          <Box columnGap={gap}>
            <Text color="cyan">{SPINNER[step % SPINNER.length]}</Text>
            <Text dimColor>{BUSY_LABEL[doing]}</Text>
          </Box>
        )}
        {advice !== null && (
          <Box paddingLeft={indent}>
            <Text color={advice.tone === 'warn' ? 'yellow' : undefined} dimColor={advice.tone === 'calm'} wrap="wrap">
              {advice.text}
            </Text>
          </Box>
        )}
        {advice?.actions !== undefined && (
          <Box paddingLeft={indent} columnGap={gap}>
            {advice.actions.map((action, i) => (
              <Button
                key={action}
                label={ACTION_LABEL[action]}
                variant={i === 0 ? 'primary' : 'secondary'}
                onPress={() => void cure($, action)}
              />
            ))}
          </Box>
        )}
      </Box>
    )

    return isEmptyBox(below) ? ours : (
      <Box flexDirection="column" rowGap={isRoomy ? 1 : 0}>
        {ours}
        {below}
      </Box>
    )
  })
}
