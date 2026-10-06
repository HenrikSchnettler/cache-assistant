// The band above the prompt, driven through the engine on each surface that
// draws it. The transcript engine (cache_status.py --json) and the model are
// answered by the test, so this covers the mod's own hooks and drawing.
import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const T0 = 1_800_000_000_000
const SURFACES = ['terminal', 'desktop'] as const
const BAND = {
  plugin: 'cache-assistant',
  component: 'AbovePrompt',
  props: {
    hasSurvey: false,
    isWorking: false,
    maxRows: 12,
    bodyColumns: 120,
    scroll: { offset: 0, bodyRows: 12, contentRows: 2 },
    view: {},
  },
} as const

// Any advice row: each one is a sentence.
const ADVICE = /\. |keep going|Switch back/

type Status = Record<string, unknown>
const warm = (over: Status = {}): Status => ({
  have_data: true,
  tier: '1h',
  ttl_seconds: 3600,
  anchor_epoch: T0 / 1000,
  rewrite_tokens: 120_000,
  ping_interval_seconds: 1800,
  transcript: '/t/s.jsonl',
  miss: null,
  ...over,
})

// The host the mod talks to: the status script, the model, the clock.
const host = (on: On, first: Status) => {
  const clock = mock.clock(on, { now: T0 })
  const seen = { status: first, runs: [] as string[][], forks: 0, readTokens: 119_000 }
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.model', () => ({ value: 'claude-opus-5-5' }))
  on('ui.toast', () => ({ value: undefined }))
  on('prompt.fill', () => ({ isFilled: true }))
  on('classic.Stop', () => ({}))
  on('classic.SessionStart', () => ({}))
  on('classic.PostModelSwitch', () => ({}))
  on('process.run', (_$, e) => {
    seen.runs.push([...e.argv])
    return { value: { exitCode: 0, stdout: JSON.stringify(seen.status), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('model.fork', () => {
    seen.forks += 1
    return {
      value: {
        isAnswered: true,
        text: 'pong',
        usage: { input_tokens: 9, output_tokens: 2, cache_read_input_tokens: seen.readTokens, cache_creation_input_tokens: 0 },
      },
    }
  })
  return { clock, seen }
}

const start = ($: Engine) =>
  $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

test('a warm window shows tier, countdown and size, and no advice', async ($, on) => {
  host(on, warm())
  await start($)
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.find({ text: /cache 1h · 60m left · ~120\.0k cached/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: ADVICE })).toBeUndefined()
    await ui.unmount()
  }
})

test('expired and large: advises against sending and offers /compact', async ($, on) => {
  host(on, warm({ anchor_epoch: T0 / 1000 - 4000 }))
  await start($)
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.find({ text: /EXPIRED · ~120\.0k to re-cache/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /\/compact first/ })).toBeDefined()
    expect(await ui.find({ key: 'fill' })).toBeDefined()
    await ui.unmount()
  }
})

test('expired but under the threshold: says to keep going', async ($, on) => {
  host(on, warm({ anchor_epoch: T0 / 1000 - 4000, rewrite_tokens: 12_000 }))
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /just keep going/ })).toBeDefined()
  expect(await ui.find({ key: 'fill' })).toBeUndefined()
})

test('the threshold is the plugin option', { options: { block_threshold_tokens: 10_000 } }, async ($, on) => {
  host(on, warm({ anchor_epoch: T0 / 1000 - 4000, rewrite_tokens: 12_000 }))
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /re-caches ~12\.0k tokens from cold/ })).toBeDefined()
})

test('the countdown redraws and warns before a large cache goes cold', async ($, on) => {
  const { clock } = host(on, warm())
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await clock.advance(57 * 60_000)
  expect(await ui.find({ text: /3:00 left/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /go cold in 3:00/ })).toBeDefined()
})

test('keep-alive pings one interval after the last touch, without a turn', async ($, on) => {
  const { clock, seen } = host(on, warm())
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'keepalive' })
  expect(await ui.find({ text: /keep-alive on 0\/12/ })).toBeDefined()
  await clock.advance(29 * 60_000)
  expect(seen.forks).toBe(0)
  seen.status = warm({ anchor_epoch: T0 / 1000 + 1800 })
  await clock.advance(61_000)
  expect(seen.forks).toBe(1)
  expect(seen.runs.at(-1)).toContain('--touch')
  expect(await ui.find({ text: /keep-alive on 1\/12/ })).toBeDefined()
})

test('keep-alive stops itself when a ping finds the cache cold', async ($, on) => {
  const { clock, seen } = host(on, warm())
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'keepalive' })
  seen.readTokens = 0
  await clock.advance(31 * 60_000)
  expect(seen.forks).toBe(1)
  expect(await ui.find({ text: /keep-alive off/ })).toBeDefined()
})

test('keep-alive stops at the ping limit', { options: { keepalive_max_pings: 2 } }, async ($, on) => {
  const { clock, seen } = host(on, warm())
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'keepalive' })
  for (const n of [1, 2, 3]) {
    seen.status = warm({ anchor_epoch: T0 / 1000 + n * 1800 })
    await clock.advance(1800_000)
  }
  expect(seen.forks).toBe(2)
  expect(await ui.find({ text: /keep-alive off/ })).toBeDefined()
})

test('a model switch on a warm cache says how long switching back stays free', async ($, on) => {
  host(on, warm())
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const switched = {
    from_model: 'claude-opus-5-5',
    to_model: 'claude-sonnet-5-5',
    requested_model: 'sonnet',
    source: 'picker',
    context_tokens: 120_000,
    prompt_cache_warm: true,
    cache_ttl: '1h',
    estimated_cache_write_usd: 1.2,
    pricing: 'catalog',
  } as const
  await $.classic.PostModelSwitch(switched)
  expect(await ui.find({ type: 'Text', text: /opus-5-5 → sonnet-5-5.*Switch back within 60m/ })).toBeDefined()
  await $.classic.PostModelSwitch({ ...switched, from_model: 'claude-sonnet-5-5', to_model: 'claude-opus-5-5' })
  expect(await ui.find({ type: 'Text', text: ADVICE })).toBeUndefined()
})

test('an unexpected cache miss is reported after the turn, with the setting that changed', async ($, on) => {
  const { seen } = host(on, warm())
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  seen.status = warm({ miss: { epoch: T0 / 1000 + 5, expected_tokens: 120_000, read_tokens: 0, model_changed: false } })
  await $.classic.Stop({ stop_hook_active: false })
  expect(await ui.find({ type: 'Text', text: /missed the warm cache \(read 0 of ~120\.0k tokens\)/ })).toBeDefined()
})

test('a forked session shows the window it inherits before its first turn', async ($, on) => {
  host(on, { have_data: false, transcript: null })
  await start($)
  await $.classic.SessionStart({
    source: 'fork',
    seconds_since_last_response: 7200,
    context_tokens: 150_000,
    prompt_cache_likely_expired: true,
  })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ text: /cache 1h · EXPIRED · ~150\.0k to re-cache/ })).toBeDefined()
})
