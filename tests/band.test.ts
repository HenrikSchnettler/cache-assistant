// The band above the prompt, driven through the engine on each surface that
// draws it. The transcript engine (cache_status.py --json) and the model are
// answered by the test, so this covers the mod's own hooks and drawing.
import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On, RenderElement } from 'claude-code'

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
  const seen = { status: first, runs: [] as string[][], forks: 0, readTokens: 119_000, commands: [] as string[], below: { type: 'Box', children: [] } as RenderElement }
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.model', () => ({ value: 'claude-opus-5-5' }))
  on('ui.toast', () => ({ value: undefined }))
  on('classic.Stop', () => ({}))
  on('classic.SessionStart', () => ({}))
  on('classic.PostModelSwitch', () => ({}))
  // The engine's own band: nothing to draw unless a test puts another plugin's there.
  on('ui.render', () => seen.below)
  on('command.run', (_$, e) => {
    seen.commands.push(e.command)
    return { text: '' }
  })
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
    expect(await ui.find({ key: 'divider' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /^─+$/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: '◷' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Cache warm · 60m left' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '1h tier · ~120.0k cached · opus-5-5' })).toBeDefined()
    expect(await ui.find({ key: 'keepalive' })).toMatchObject({ props: { label: 'Keep-alive off' } })
    expect(await ui.find({ key: 'hide' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: ADVICE })).toBeUndefined()
    await ui.unmount()
  }
})

test('expired and large: advises against sending and offers compact and clear', async ($, on) => {
  host(on, warm({ anchor_epoch: T0 / 1000 - 4000 }))
  await start($)
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.find({ type: 'Text', text: 'Cache expired' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /~120\.0k to re-cache/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /compact first/ })).toBeDefined()
    expect(await ui.find({ key: 'compact' })).toBeDefined()
    expect(await ui.find({ key: 'clear' })).toBeDefined()
    expect(await ui.find({ key: 'send' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /twice/ })).toBeUndefined()
    // Nothing left to keep alive: drawn as a state in the detail line, not a control.
    expect(await ui.find({ key: 'keepalive' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: '1h tier · ~120.0k to re-cache · opus-5-5 · keep-alive off' })).toBeDefined()
    await ui.unmount()
  }
})

test('the compact button runs /compact and re-reads the window', async ($, on) => {
  const { seen } = host(on, warm({ anchor_epoch: T0 / 1000 - 4000 }))
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const before = seen.runs.length
  seen.status = { have_data: false, transcript: '/t/s.jsonl' }
  await ui.press({ key: 'compact' })
  expect(seen.commands).toContain('compact')
  expect(seen.runs.length).toBe(before + 1)
  expect(await ui.find({ key: 'compact' })).toBeUndefined()
})

test('the clear button runs /clear', async ($, on) => {
  const { seen } = host(on, warm({ anchor_epoch: T0 / 1000 - 4000 }))
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  seen.status = { have_data: false, transcript: null }
  await ui.press({ key: 'clear' })
  expect(seen.commands).toContain('clear')
  expect(await ui.find({ key: 'clear' })).toBeUndefined()
})

test('expired but under the threshold: says to keep going', async ($, on) => {
  host(on, warm({ anchor_epoch: T0 / 1000 - 4000, rewrite_tokens: 12_000 }))
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /just keep going/ })).toBeDefined()
  expect(await ui.find({ key: 'compact' })).toBeUndefined()
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
  expect(await ui.find({ type: 'Text', text: 'Cache warm · 3:00 left' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /go cold in 3:00/ })).toBeDefined()
})

test('keep-alive pings one interval after the last touch, without a turn', async ($, on) => {
  const { clock, seen } = host(on, warm())
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'keepalive' })
  expect(await ui.find({ key: 'keepalive' })).toMatchObject({ props: { label: 'Keep-alive on 0/12' } })
  await clock.advance(29 * 60_000)
  expect(seen.forks).toBe(0)
  seen.status = warm({ anchor_epoch: T0 / 1000 + 1800 })
  await clock.advance(61_000)
  expect(seen.forks).toBe(1)
  expect(seen.runs.at(-1)).toContain('--touch')
  expect(await ui.find({ key: 'keepalive' })).toMatchObject({ props: { label: 'Keep-alive on 1/12' } })
})

test('keep-alive stops itself when a ping finds the cache cold', async ($, on) => {
  const { clock, seen } = host(on, warm())
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'keepalive' })
  seen.readTokens = 0
  await clock.advance(31 * 60_000)
  expect(seen.forks).toBe(1)
  expect(await ui.find({ key: 'keepalive' })).toMatchObject({ props: { label: 'Keep-alive off' } })
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
  expect(await ui.find({ key: 'keepalive' })).toMatchObject({ props: { label: 'Keep-alive off' } })
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
  expect(await ui.find({ type: 'Text', text: 'Cache expired' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /~150\.0k to re-cache/ })).toBeDefined()
})

test('a switch on a cold, large cache still offers compact and clear', async ($, on) => {
  host(on, warm({ anchor_epoch: T0 / 1000 - 4000 }))
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await $.classic.PostModelSwitch({
    from_model: 'claude-fable-5',
    to_model: 'claude-opus-5-5',
    requested_model: 'opus',
    source: 'picker',
    context_tokens: 120_000,
    prompt_cache_warm: false,
    cache_ttl: '1h',
    estimated_cache_write_usd: 1.2,
    pricing: 'catalog',
  })
  expect(await ui.find({ type: 'Text', text: /re-caches ~120\.0k tokens from cold/ })).toBeDefined()
  expect(await ui.find({ key: 'compact' })).toBeDefined()
})

test('while Claude works the band shows a placeholder and no controls', async ($, on) => {
  host(on, warm({ anchor_epoch: T0 / 1000 - 4000 }))
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', () => ({ text: '' }))
  await start($)
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    await $.turn.start({ text: 'hi', turnId: 't1' })
    expect(await ui.find({ type: 'Text', text: 'Available when Claude is done' })).toBeDefined()
    // Only a compaction is animated.
    expect(await ui.find({ type: 'Text', text: /^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]$/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: 'Cache in use' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '1h tier · opus-5-5 · keep-alive off' })).toBeDefined()
    for (const key of ['compact', 'clear', 'keepalive']) expect(await ui.find({ key })).toBeUndefined()
    await $.turn.complete({ reason: 'aborted', answer: '', durationMs: 5, isAborted: true, turnId: 't1' })
    expect(await ui.find({ type: 'Text', text: 'Available when Claude is done' })).toBeUndefined()
    expect(await ui.find({ key: 'compact' })).toBeDefined()
    await ui.unmount()
  }
})

test('a compaction shows its own placeholder and locks the buttons', async ($, on) => {
  host(on, warm({ anchor_epoch: T0 / 1000 - 4000 }))
  const during: boolean[] = []
  const seat: { ui?: { find: (q: { type?: 'Text'; key?: string; text?: string }) => Promise<unknown> } } = {}
  on('session.compact', async () => {
    const ui = seat.ui
    if (ui === undefined) throw new Error('not mounted')
    during.push((await ui.find({ type: 'Text', text: /^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] Compacting the conversation…$/ })) !== undefined)
    during.push((await ui.find({ key: 'compact' })) === undefined)
    return { messages: [{ role: 'user' as const, text: 'summary', toolUses: [] }] }
  })
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  seat.ui = ui
  const summary = { role: 'user' as const, text: 'old', toolUses: [] }
  await $.session.compact({ trigger: 'manual', messages: [summary] })
  expect(during).toEqual([true, true])
  expect(await ui.find({ type: 'Text', text: /Compacting the conversation…/ })).toBeUndefined()
})

test('Allow send anyway arms the guard for the next message', async ($, on) => {
  const { seen } = host(on, warm({ anchor_epoch: T0 / 1000 - 4000 }))
  await start($)
  expect(seen.runs[0]).toEqual(expect.arrayContaining(['--band', 'on']))
  for (const surface of SURFACES) {
    // The ack is spent by a message; the next read shows the button again.
    seen.status = warm({ anchor_epoch: T0 / 1000 - 4000, armed: false })
    await $.classic.Stop({ stop_hook_active: false })
    const ui = await $.ui.mount({ ...BAND, surface })
    seen.status = warm({ anchor_epoch: T0 / 1000 - 4000, armed: true })
    await ui.press({ key: 'send' })
    expect(seen.runs.at(-1)).toContain('--allow-next')
    expect(await ui.find({ type: 'Text', text: /your next message goes through/ })).toBeDefined()
    expect(await ui.find({ key: 'send' })).toBeUndefined()
    expect(await ui.find({ key: 'compact' })).toBeDefined()
    await ui.unmount()
  }
})

test('a model switch the guard blocked offers Allow send anyway', async ($, on) => {
  const { seen } = host(on, warm())
  on('prompt.submit', (_$, e) => ({ text: e.text }))
  await start($)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  seen.status = warm({ blocked: 'model_switch' })
  await $.prompt.submit({ text: 'go on', wait: false, origin: { kind: 'composer' } })
  expect(await ui.find({ type: 'Text', text: /You changed the model,/ })).toBeDefined()
  expect(await ui.find({ key: 'send' })).toBeDefined()
})

test('the hidden band hands the acknowledgement back to a re-send', async ($, on) => {
  const { seen } = host(on, warm())
  await start($)
  await $.command.run({
    command: 'cache-band',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 120 },
  })
  expect(seen.runs.at(-1)).toEqual(expect.arrayContaining(['--band', 'off']))
})

test('an empty cache reads like every other state', async ($, on) => {
  host(on, { have_data: false, transcript: '/t/s.jsonl' })
  await start($)
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.find({ type: 'Text', text: '◷' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Cache empty' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'fills with your next message · opus-5-5 · keep-alive off' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /\?|warming/ })).toBeUndefined()
    await ui.unmount()
  }
})

test("another plugin's band is drawn under ours, not replaced", async ($, on) => {
  const { seen } = host(on, warm())
  seen.below = { type: 'Text', children: ['other plugin band'] }
  await start($)
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.find({ type: 'Text', text: 'Cache warm · 60m left' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'other plugin band' })).toBeDefined()
    // One rule between the two bands, never wider than the band: on a
    // terminal exactly its columns, on a desktop the surface's own rule.
    if (surface === 'terminal') {
      expect(await ui.findAll({ type: 'Text', text: /^─+$/ })).toHaveLength(1)
      expect(await ui.find({ type: 'Text', text: /^─+$/ })).toMatchObject({ text: '─'.repeat(BAND.props.bodyColumns) })
    } else {
      expect(await ui.findAll({ type: 'Markdown', text: '---' })).toHaveLength(1)
      expect(await ui.find({ type: 'Text', text: /─/ })).toBeUndefined()
    }
    await ui.unmount()
  }
})

test('no rule is drawn when nothing but the engine sits beneath the band', async ($, on) => {
  const { seen } = host(on, warm())
  seen.below = { type: 'engine', ref: 0 } as unknown as typeof seen.below
  await start($)
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.find({ type: 'Text', text: 'Cache warm · 60m left' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^─+$/ })).toBeUndefined()
    expect(await ui.find({ type: 'Markdown', text: '---' })).toBeUndefined()
    await ui.unmount()
  }
})
