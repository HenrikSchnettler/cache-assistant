// Cache Assistant — the band above the prompt.
//
// Shows what the status line shows (tier, countdown, cold re-write size) plus
// advice on what to do next and a keep-alive switch. The cache window itself
// still comes from lib/cache_core.py (via statusline/cache_status.py --json),
// read once per turn and on the events that move it; the countdown between
// reads is plain arithmetic on the anchor, so nothing runs per second but a
// string compare.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { CacheView, KeepAlive, Notice, SessionRef } from '../types'
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

const PING = 'Cache keep-alive ping. Reply with the single word "pong" and nothing else.'

type StatusJson = {
  have_data?: boolean
  tier?: '5m' | '1h' | null
  ttl_seconds?: number | null
  anchor_epoch?: number | null
  rewrite_tokens?: number | null
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
let isPinging = false
// The newest miss already reported (or older than this load).
let missSeenMs = 0
// Settings changed since the last turn: the suspects for a cache miss.
let changed: string[] = []

// Reads the window from the transcript through the plugin's own engine.
// `touch` first records a keep-alive hit that left no transcript line.
const refresh = async ($: EngineInterface, touch = false): Promise<CacheView | null> => {
  const id = await $.session.id()
  const known = await read($, session)
  const transcript = known?.id === id ? known.transcript : null
  const argv = ['python3', `${$.plugin.root}/statusline/cache_status.py`, '--json', '--session', id]
  if (transcript) argv.push('--transcript', transcript)
  if (touch) argv.push('--touch')

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
    await refresh($, true)
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
    const view = await refresh($)
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

    return { text: hidden ? 'Cache Assistant band hidden.' : 'Cache Assistant band shown.' }
  })

  on('turn.start', async ($, e, next) => {
    isWorking = true
    // A message from the person: the keep-alive budget starts over.
    await update($, keepAlive, (k: KeepAlive) => (k.pings === 0 ? k : { ...k, pings: 0 }))

    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId === undefined) {
      isWorking = false
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

  on('session.compact', async ($, e, next) => {
    const compacted = await next(e)
    if (e.agentId === undefined) await refresh($)

    return compacted
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

    const left = view ? remainingMs(view, now) : 0
    const isExpired = view !== null && view.ttlMs !== null && left <= 0
    const isClosing = view !== null && view.ttlMs !== null && !isExpired && left <= Math.max(30_000, view.ttlMs * 0.1)
    const color = view === null || view.ttlMs === null ? undefined : isExpired ? 'red' : isClosing ? 'yellow' : 'green'
    const tier = view?.tier ?? '?'
    const headline =
      view === null
        ? '⚡ cache · warming…'
        : view.ttlMs === null
          ? `⚡ cache ${tier} · ?`
          : isExpired
            ? `⚡ cache ${tier} · EXPIRED · ~${fmtTokens(view.tokens)} to re-cache`
            : `⚡ cache ${tier} · ${fmtLeft(left)} left · ~${fmtTokens(view.tokens)} cached`
    const advice = advise({ view, now, threshold, note, isKeepAliveOn: alive.isOn })

    return (
      <Box flexDirection="column" width={e.props.bodyColumns}>
        <Box columnGap={1} flexWrap="wrap">
          <Text color={color} dimColor={color === undefined}>
            {headline}
          </Text>
          <Text dimColor>· {model}</Text>
          <Button
            key="keepalive"
            label={alive.isOn ? `keep-alive on ${alive.pings}/${maxPings}` : 'keep-alive off'}
            variant={alive.isOn ? 'primary' : 'secondary'}
            onPress={async () => {
              const said = await toggleKeepAlive($)
              if (!(await read($, keepAlive)).isOn && !said.endsWith('is off.')) $.ui.toast(said)
            }}
          />
          {advice?.fill !== undefined && (
            <Button
              key="fill"
              label={advice.fill}
              onPress={() => void $.prompt.fill({ text: advice.fill ?? '' })}
            />
          )}
          <Button key="hide" label="hide" role="dismiss" onPress={() => update($, isHidden, () => true)} />
        </Box>
        {advice !== null && (
          <Text color={advice.tone === 'warn' ? 'yellow' : undefined} dimColor={advice.tone === 'calm'} wrap="wrap">
            {advice.text}
          </Text>
        )}
      </Box>
    )
  })
}
