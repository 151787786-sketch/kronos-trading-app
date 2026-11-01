// DeepSeek Harness (dsh) plugin: A-share realtime watch.
//
// Registers three tools backed by the free Tencent quote endpoint
// (https://qt.gtimg.cn/q=), the same source the Kronos trading app uses:
//
//   kronos_quote   — batch realtime quotes for one or more A-share symbols
//   kronos_watch   — scan a watchlist for buy/sell threshold hits (red/green)
//   kronos_status  — market session, data freshness, and last-request stats
//
// Why a plugin rather than a skill: a registered tool schema reaches the model
// on every request, so there is no trigger gamble.
//
// Design notes:
//   · node builtins only (no runtime dependencies) — the plugin cannot drift
//     from a dependency tree, and installs fast.
//   · The quote endpoint returns gb18030; Node decodes it with TextDecoder.
//   · Field 30 of the ~-separated payload is the quote timestamp
//     (YYYYMMDDHHMMSS). It is the ONLY freshness source: DSA's update_time is
//     deliberately not used.
//   · Every failure path returns a structured result. A network error must not
//     throw out of a tool call; the model needs to read what happened.

const QT_URL = 'https://qt.gtimg.cn/q='

// Field positions, verified against a live payload (88 fields).
const F_NAME = 1
const F_CODE = 2
const F_PRICE = 3
const F_PREV_CLOSE = 4
const F_OPEN = 5
const F_TIME = 30
const F_CHANGE = 31
const F_PCT = 32
const F_HIGH = 33
const F_LOW = 34
const F_VOLUME = 36
const F_AMOUNT = 37
const F_TURNOVER = 38
const F_AMPLITUDE = 43
const F_VOL_RATIO = 46

/** Single batch cap. Tencent tolerates far more, but a bounded batch keeps
 *  one tool call from becoming an accidental bulk download. */
const MAX_BATCH = 500

/** Quote cache lifetime. Five seconds matches the Kronos app and keeps a
 *  thinking loop from re-hitting the endpoint on every step. */
const CACHE_TTL_MS = 5_000

/** Minimum gap between request starts, enforced across concurrent callers. */
const MIN_INTERVAL_MS = 500

const QUOTE_TIMEOUT_MS = 12_000

// ---------------------------------------------------------------------------
// Symbols
// ---------------------------------------------------------------------------

export function marketPrefix(raw) {
  const s = String(raw ?? '').trim()
  if (/^(sh|sz|bj)/i.test(s)) return s.slice(0, 2).toLowerCase()
  const code = s.padStart(6, '0')
  const head = code[0]
  if (head === '6' || head === '9' || head === '5') return 'sh'
  if (head === '4' || head === '8') return 'bj'
  return 'sz'
}

export function fullSymbol(raw) {
  const s = String(raw ?? '').trim()
  if (/^(sh|sz|bj)/i.test(s)) return s.toLowerCase()
  return marketPrefix(s) + s.padStart(6, '0')
}

export function bareCode(raw) {
  const s = String(raw ?? '').trim()
  if (/^(sh|sz|bj)/i.test(s)) return s.slice(2).padStart(6, '0')
  return s.padStart(6, '0')
}

/** Accept "600126", "sh600126", "600126,000657", or a whitespace list. */
export function parseSymbols(input) {
  if (input == null) return []
  const parts = Array.isArray(input) ? input : String(input).split(/[\s,，、;；]+/)
  const seen = new Set()
  const out = []
  for (const p of parts) {
    const raw = String(p ?? '').trim()
    if (!raw) continue
    const full = fullSymbol(raw)
    if (seen.has(full)) continue
    seen.add(full)
    out.push(full)
  }
  return out
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

const LINE_RE = /v_(\w+)="([^"]*)";/g

function num(v) {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

/** Parse the raw gb18030-decoded body into quote records. Pure; no network. */
export function parseQuoteText(text) {
  const out = []
  const src = String(text ?? '')
  LINE_RE.lastIndex = 0
  let m
  while ((m = LINE_RE.exec(src)) !== null) {
    const sym = m[1]
    const f = m[2].split('~')
    if (f.length < 35) {
      out.push({
        symbol: bareCode(sym),
        fullSymbol: sym.toLowerCase(),
        ok: false,
        error: `field count ${f.length} is too short — the code may have no quote (suspended, delisted, or wrong)`,
      })
      continue
    }
    const quoteTime = String(f[F_TIME] ?? '').trim()
    out.push({
      symbol: f[F_CODE] || bareCode(sym),
      fullSymbol: sym.toLowerCase(),
      name: f[F_NAME],
      price: num(f[F_PRICE]),
      prevClose: num(f[F_PREV_CLOSE]),
      open: num(f[F_OPEN]),
      quoteTime,
      change: num(f[F_CHANGE]),
      pct: num(f[F_PCT]),
      high: num(f[F_HIGH]),
      low: num(f[F_LOW]),
      volume: num(f[F_VOLUME]),
      amount: num(f[F_AMOUNT]),
      turnover: num(f[F_TURNOVER]),
      amplitude: f.length > F_AMPLITUDE ? num(f[F_AMPLITUDE]) : null,
      volumeRatio: f.length > F_VOL_RATIO ? num(f[F_VOL_RATIO]) : null,
      ok: true,
    })
  }
  return out
}

/** '20261019142647' -> epoch ms, or null when unparseable. */
export function parseQuoteTime(raw) {
  const s = String(raw ?? '').trim()
  if (s.length < 14 || !/^\d{14}/.test(s)) return null
  const y = Number(s.slice(0, 4))
  const mo = Number(s.slice(4, 6))
  const d = Number(s.slice(6, 8))
  const h = Number(s.slice(8, 10))
  const mi = Number(s.slice(10, 12))
  const se = Number(s.slice(12, 14))
  const t = new Date(y, mo - 1, d, h, mi, se).getTime()
  return Number.isFinite(t) ? t : null
}

/** Seconds since the quote timestamp (field 30). Null when unparseable. */
export function quoteAgeSec(quote, now = Date.now()) {
  const t = parseQuoteTime(quote?.quoteTime)
  if (t == null) return null
  return Math.round((now - t) / 1000)
}

// ---------------------------------------------------------------------------
// Market session (Asia/Shanghai)
// ---------------------------------------------------------------------------

function shanghaiParts(date = new Date()) {
  // Intl gives the correct wall clock regardless of where the host runs.
  const fmt = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
  const parts = Object.fromEntries(fmt.formatToParts(date).map((p) => [p.type, p.value]))
  const weekday = parts.weekday
  const minutes = Number(parts.hour) * 60 + Number(parts.minute)
  return { weekday, minutes }
}

export function tradingSession(now = new Date()) {
  const { weekday, minutes } = shanghaiParts(now)
  if (weekday === 'Sat' || weekday === 'Sun') {
    return { open: false, phase: 'closed', reason: 'weekend' }
  }
  if (minutes < 9 * 60 + 30) return { open: false, phase: 'pre', reason: 'before open' }
  if (minutes <= 11 * 60 + 30) return { open: true, phase: 'am', reason: 'morning session' }
  if (minutes < 13 * 60) return { open: false, phase: 'noon', reason: 'lunch break' }
  if (minutes <= 15 * 60) return { open: true, phase: 'pm', reason: 'afternoon session' }
  return { open: false, phase: 'post', reason: 'after close' }
}

// ---------------------------------------------------------------------------
// HTTP with throttle + cache
// ---------------------------------------------------------------------------

function createQuoteClient(config) {
  const cache = new Map()
  const stats = { requests: 0, errors: 0, cacheHits: 0, batches: 0, parsed: 0 }
  let lastStart = 0

  const interval = Math.max(0, Number(config?.intervalMs ?? MIN_INTERVAL_MS) || 0)
  const ttl = Math.max(0, Number(config?.cacheTtlMs ?? CACHE_TTL_MS) || 0)
  const timeout = Math.max(1000, Number(config?.timeoutMs ?? QUOTE_TIMEOUT_MS) || QUOTE_TIMEOUT_MS)

  async function throttle() {
    const gap = interval - (Date.now() - lastStart)
    if (gap > 0) await new Promise((r) => setTimeout(r, gap))
    lastStart = Date.now()
  }

  async function fetchBatch(fulls, { useCache = true } = {}) {
    const now = Date.now()
    const out = new Map()
    const need = []
    for (const f of fulls) {
      const hit = cache.get(f)
      if (useCache && hit && now - hit.at <= ttl) {
        stats.cacheHits += 1
        out.set(f, hit.quote)
      } else {
        need.push(f)
      }
    }

    if (need.length > 0) {
      stats.batches += 1
      await throttle()
      const url = QT_URL + need.join(',')
      let text = null
      let error = null
      try {
        stats.requests += 1
        const res = await fetch(url, {
          signal: AbortSignal.timeout(timeout),
          headers: {
            // The endpoint answers to a browser UA; some edges reject others.
            'User-Agent': 'Mozilla/5.0',
            Referer: 'https://gu.qq.com/',
          },
        })
        if (!res.ok) {
          error = `HTTP ${res.status}`
        } else {
          const buf = Buffer.from(await res.arrayBuffer())
          text = new TextDecoder('gb18030').decode(buf)
        }
      } catch (e) {
        error = e?.name === 'TimeoutError' ? `timeout after ${timeout}ms` : `${e?.name}: ${e?.message ?? e}`
      }

      if (text != null) {
        const parsed = parseQuoteText(text)
        stats.parsed += parsed.filter((q) => q.ok).length
        const byFull = new Map(parsed.map((q) => [q.fullSymbol, q]))
        for (const f of need) {
          const q = byFull.get(f) ?? {
            symbol: bareCode(f),
            fullSymbol: f,
            ok: false,
            error: 'not present in the response',
          }
          out.set(f, q)
          if (q.ok) cache.set(f, { at: Date.now(), quote: q })
        }
      } else {
        stats.errors += 1
        for (const f of need) {
          out.set(f, { symbol: bareCode(f), fullSymbol: f, ok: false, error })
        }
      }
    }

    return fulls.map((f) => out.get(f)).filter(Boolean)
  }

  return {
    async fetch(symbols, opts) {
      const fulls = parseSymbols(symbols)
      if (fulls.length === 0) return { quotes: [], error: 'no symbols given' }
      if (fulls.length > MAX_BATCH) {
        return { quotes: [], error: `batch cap is ${MAX_BATCH} symbols, got ${fulls.length}` }
      }
      const quotes = await fetchBatch(fulls, opts)
      return { quotes }
    },
    stats: () => ({ ...stats, cacheSize: cache.size, intervalMs: interval, cacheTtlMs: ttl }),
  }
}

// ---------------------------------------------------------------------------
// Alert evaluation
// ---------------------------------------------------------------------------

export const DEFAULT_RULES = {
  pctUp: 3.0,
  pctDown: -3.0,
  volumeRatio: 2.5,
  turnover: 5.0,
}

/**
 * Decide whether one quote hits a buy or sell threshold.
 *
 * A-share convention: red = up (sell side), green = down (buy side).
 * Returns null when nothing fires.
 */
export function evaluate(quote, rules = {}) {
  if (!quote || !quote.ok || quote.pct == null) return null
  const r = { ...DEFAULT_RULES, ...rules }
  const pct = quote.pct
  const vr = quote.volumeRatio ?? 0
  const to = quote.turnover ?? 0

  const reasons = []
  let side = null
  if (pct <= r.pctDown) {
    side = 'buy'
    reasons.push(`change ${pct.toFixed(2)}% <= ${r.pctDown}% (reached the buy zone)`)
  } else if (pct >= r.pctUp) {
    side = 'sell'
    reasons.push(`change ${pct.toFixed(2)}% >= ${r.pctUp}% (reached the sell zone)`)
  }

  if (side === null) {
    if (vr >= r.volumeRatio) {
      return {
        symbol: quote.symbol,
        name: quote.name,
        side: 'watch',
        level: 1,
        flash: null,
        reasons: [`volume ratio ${vr.toFixed(2)} >= ${r.volumeRatio} (heavy volume, move still small)`],
        quoteTime: quote.quoteTime,
        price: quote.price,
        pct,
        volumeRatio: vr,
      }
    }
    return null
  }

  let level = 2
  if (vr >= r.volumeRatio) {
    reasons.push(`volume ratio ${vr.toFixed(2)} >= ${r.volumeRatio} (confirms the move)`)
    level = 3
  } else if (to >= r.turnover) {
    reasons.push(`turnover ${to.toFixed(2)}% >= ${r.turnover}% (unusual activity)`)
    level = 3
  }

  return {
    symbol: quote.symbol,
    name: quote.name,
    side,
    level,
    levelLabel: level === 3 ? 'critical' : 'warning',
    flash: side === 'buy' ? 'green' : 'red',
    reasons,
    quoteTime: quote.quoteTime,
    price: quote.price,
    pct,
    volumeRatio: vr,
    turnover: to,
  }
}

// ---------------------------------------------------------------------------
// Tool rendering
// ---------------------------------------------------------------------------

function fmtPct(v) {
  if (v == null) return '-'
  return (v > 0 ? '+' : '') + v.toFixed(2) + '%'
}

function fmtNum(v, digits = 2) {
  if (v == null) return '-'
  return Number(v).toFixed(digits)
}

function renderQuoteTable(quotes, session, now) {
  const lines = []
  lines.push(`Market: ${session.open ? 'OPEN' : 'CLOSED'} (${session.reason})`)
  lines.push('')
  lines.push('| code | name | price | change | high | low | vol-ratio | turnover | quote time | age |')
  lines.push('| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |')
  for (const q of quotes) {
    if (!q.ok) {
      lines.push(`| ${q.symbol} | — | — | — | — | — | — | — | — | ${q.error ?? 'no quote'} |`)
      continue
    }
    const age = quoteAgeSec(q, now)
    lines.push(
      `| ${q.symbol} | ${q.name} | ${fmtNum(q.price, 3)} | ${fmtPct(q.pct)} | ${fmtNum(q.high)} | ${fmtNum(q.low)} | ${fmtNum(q.volumeRatio)} | ${fmtNum(q.turnover)}% | ${q.quoteTime} | ${age == null ? '-' : age + 's'} |`,
    )
  }
  lines.push('')
  lines.push('Freshness is the exchange timestamp from the payload (field 30), not a local clock or a cached update_time.')
  return lines.join('\n')
}

function renderAlerts(alerts, checked) {
  if (alerts.length === 0) {
    return `Checked ${checked} symbol(s): no threshold hit at the current rules.`
  }
  const lines = [`Checked ${checked} symbol(s): ${alerts.length} hit(s).`, '']
  for (const a of alerts) {
    const badge = a.side === 'buy' ? 'GREEN (buy)' : a.side === 'sell' ? 'RED (sell)' : 'AMBER (watch)'
    lines.push(`**${a.name ?? a.symbol} (${a.symbol})** — ${badge}, level ${a.level}`)
    for (const r of a.reasons) lines.push(`  · ${r}`)
    lines.push(`  · price ${fmtNum(a.price, 3)}, quote time ${a.quoteTime}`)
    lines.push('')
  }
  lines.push('Buy-side hits flash green and sell-side hits flash red, following the A-share convention (red = up).')
  return lines.join('\n')
}

// ---------------------------------------------------------------------------
// Plugin registration
// ---------------------------------------------------------------------------

export const name = 'kronos-watch'

/** tools is the only hard dependency; the rest are optional surfaces. */
export const inject = ['tools']

export function apply(ctx, config = {}) {
  const client = createQuoteClient(config)

  const quoteTool = {
    name: config.toolName || 'kronos_quote',
    description:
      'Fetch realtime A-share quotes in one batch. Supports Shanghai (6/9/5), Shenzhen (0/3) and Beijing (4/8) codes; accepts "600126", "sh600126", or a comma/space separated list. Returns price, change, high, low, volume ratio, turnover, and the exchange quote timestamp. Use this before answering any question about a current A-share price or intraday move — do not answer from memory. If a symbol looks stale, compare the quote time with the current time in Asia/Shanghai; outside trading hours the timestamp legitimately stays at the previous close.',
    parameters: {
      type: 'object',
      properties: {
        symbols: {
          type: 'string',
          description:
            'One or more A-share symbols, comma or space separated. Examples: "600519", "600519,000001", "sh600519 sz000001"',
        },
      },
      required: ['symbols'],
    },
    execute: async (args) => {
      const now = Date.now()
      const session = tradingSession(new Date(now))
      const { quotes, error } = await client.fetch(args?.symbols)
      if (error) return { ok: false, error }
      return {
        ok: true,
        session,
        serverTime: new Date(now).toISOString(),
        quotes: quotes.map((q) => ({ ...q, ageSec: q.ok ? quoteAgeSec(q, now) : null })),
        source: 'qt.gtimg.cn',
        timeBasis: 'field 30 (exchange quote timestamp)',
      }
    },
    render: (_args, value) => {
      if (!value?.ok) return [{ type: 'text', text: `Quote failed: ${value?.error ?? 'unknown error'}` }]
      const now = Date.now()
      return [{ type: 'text', text: renderQuoteTable(value.quotes ?? [], value.session, now) }]
    },
    presentCall: (args) => ({
      card: 'generic',
      title: 'kronos_quote',
      kind: 'read',
      rawInput: args,
    }),
  }

  const watchTool = {
    name: config.watchToolName || 'kronos_watch',
    description:
      'Scan A-share symbols against buy/sell thresholds and report hits. Buy-side hits flash green, sell-side hits flash red (A-share convention: red = up = take profit, green = down = buy the dip). Thresholds default to +/-3% with a 2.5x volume-ratio confirmation; override them per call when the user asks for a different sensitivity. Use this to answer "does anything need my attention right now" style questions.',
    parameters: {
      type: 'object',
      properties: {
        symbols: {
          type: 'string',
          description: 'One or more A-share symbols, comma or space separated.',
        },
        pctUp: { type: 'number', description: 'Sell-side change threshold in percent (default 3.0).' },
        pctDown: { type: 'number', description: 'Buy-side change threshold in percent (default -3.0).' },
        volumeRatio: { type: 'number', description: 'Volume-ratio confirmation level (default 2.5).' },
        turnover: { type: 'number', description: 'Turnover percent that confirms a move (default 5.0).' },
      },
      required: ['symbols'],
    },
    execute: async (args) => {
      const rules = {
        pctUp: args?.pctUp ?? DEFAULT_RULES.pctUp,
        pctDown: args?.pctDown ?? DEFAULT_RULES.pctDown,
        volumeRatio: args?.volumeRatio ?? DEFAULT_RULES.volumeRatio,
        turnover: args?.turnover ?? DEFAULT_RULES.turnover,
      }
      const { quotes, error } = await client.fetch(args?.symbols)
      if (error) return { ok: false, error }
      const alerts = quotes.map((q) => evaluate(q, rules)).filter(Boolean)
      return {
        ok: true,
        rules,
        session: tradingSession(new Date()),
        checked: quotes.length,
        alerts,
        source: 'qt.gtimg.cn',
      }
    },
    render: (_args, value) => {
      if (!value?.ok) return [{ type: 'text', text: `Watch failed: ${value?.error ?? 'unknown error'}` }]
      return [{ type: 'text', text: renderAlerts(value.alerts ?? [], value.checked ?? 0) }]
    },
    presentCall: (args) => ({
      card: 'generic',
      title: 'kronos_watch',
      kind: 'read',
      rawInput: args,
    }),
    isConcurrencySafe: () => true,
  }

  const statusTool = {
    name: config.statusToolName || 'kronos_status',
    description:
      'Report the A-share market session, the quote client stats (requests, cache hits, errors) and which symbols are cached. Use this when the user asks whether the market is open, why a quote looks stale, or whether the quote endpoint is reachable.',
    parameters: { type: 'object', properties: {}, required: [] },
    execute: async () => ({
      ok: true,
      session: tradingSession(new Date()),
      serverTime: new Date().toISOString(),
      stats: client.stats(),
      source: 'qt.gtimg.cn',
      timeBasis: 'field 30 (exchange quote timestamp)',
      batchCap: MAX_BATCH,
    }),
    render: (_args, value) => {
      const s = value?.stats ?? {}
      const lines = [
        `Market: ${value?.session?.open ? 'OPEN' : 'CLOSED'} (${value?.session?.reason})`,
        `Requests: ${s.requests ?? 0} · batches ${s.batches ?? 0} · cache hits ${s.cacheHits ?? 0} · errors ${s.errors ?? 0}`,
        `Throttle: ${s.intervalMs ?? 0}ms between request starts · cache TTL ${s.cacheTtlMs ?? 0}ms · batch cap ${value?.batchCap ?? MAX_BATCH}`,
        `Cached symbols: ${s.cacheSize ?? 0}`,
        `Source: ${value?.source ?? 'qt.gtimg.cn'} · freshness basis: ${value?.timeBasis ?? 'field 30'}`,
      ]
      return [{ type: 'text', text: lines.join('\n') }]
    },
    presentCall: (args) => ({ card: 'generic', title: 'kronos_status', kind: 'read', rawInput: args }),
    isConcurrencySafe: () => true,
  }

  for (const tool of [quoteTool, watchTool, statusTool]) {
    try {
      ctx.tools.register(tool)
    } catch (error) {
      // A duplicate name or a surface change must not take the whole plugin down.
      console.error(`[kronos-watch] ${tool.name} registration skipped: ${error}`)
    }
  }
}

// Reachable from a test suite; the tools themselves are the product.
export const __internal = {
  createQuoteClient,
  marketPrefix,
  fullSymbol,
  bareCode,
  parseSymbols,
  parseQuoteText,
  parseQuoteTime,
  quoteAgeSec,
  tradingSession,
  evaluate,
  DEFAULT_RULES,
  MAX_BATCH,
  CACHE_TTL_MS,
  MIN_INTERVAL_MS,
}
