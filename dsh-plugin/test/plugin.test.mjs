import { __internal as I, name, inject, apply } from '../dsh/index.js'

const ok = []
const bad = []
function check(label, cond, detail = '') {
  if (cond) { ok.push(label); console.log('  OK ' + label) }
  else { bad.push(label); console.log('  XX ' + label + (detail ? ' — ' + detail : '')) }
}

// 代理清理由调用 shell 负责（Node 的 fetch 读 HTTPS_PROXY）

console.log('=== 1. 插件契约 ===')
check('导出 name', name === 'kronos-watch')
check('导出 inject 含 tools', Array.isArray(inject) && inject.includes('tools'))
check('导出 apply 为函数', typeof apply === 'function')

console.log('\n=== 2. 代码前缀 ===')
check('600519 -> sh600519', I.fullSymbol('600519') === 'sh600519')
check('000001 -> sz000001', I.fullSymbol('000001') === 'sz000001')
check('300750 -> sz300750', I.fullSymbol('300750') === 'sz300750')
check('512480(ETF) -> sh512480', I.fullSymbol('512480') === 'sh512480')
check('430047(北交所) -> bj430047', I.fullSymbol('430047') === 'bj430047')
check('已带前缀原样', I.fullSymbol('sh600519') === 'sh600519')
check('bareCode 去前缀', I.bareCode('sz000001') === '000001')

console.log('\n=== 3. 符号解析 ===')
check('逗号分隔', I.parseSymbols('600519,000001').length === 2)
check('空格分隔', I.parseSymbols('600519 000001').length === 2)
check('中文逗号', I.parseSymbols('600519，000001').length === 2)
check('去重', I.parseSymbols('600519,600519,sh600519').length === 1)
check('空输入', I.parseSymbols('').length === 0)
check('数组输入', I.parseSymbols(['600519', '000001']).length === 2)

console.log('\n=== 4. 解析真实格式 ===')
const SAMPLE = 'v_sh600126="1~杭钢股份~600126~5.75~5.73~5.70~147932~77916~70012~5.74~2981~5.73~1660~5.72~1841~5.71~2236~5.70~1490~5.75~777~5.76~3041~5.77~1943~5.78~2010~5.79~4116~~20261019142647~0.02~0.35~5.81~5.68~5.75/147932/85097427~147932~8510~0.44~128.71~~5.81~5.68~2.27~194.19~194.19~1.00~";'
const p = I.parseQuoteText(SAMPLE)
check('解析出 1 条', p.length === 1)
if (p[0]?.ok) {
  check('名称', p[0].name === '杭钢股份', p[0].name)
  check('代码', p[0].symbol === '600126')
  check('价格 5.75', p[0].price === 5.75)
  check('昨收 5.73', p[0].prevClose === 5.73)
  check('行情时间取自 field 30', p[0].quoteTime === '20261019142647', p[0].quoteTime)
  check('涨跌幅 0.35', p[0].pct === 0.35)
  check('高 5.81 / 低 5.68', p[0].high === 5.81 && p[0].low === 5.68)
  check('换手 0.44', p[0].turnover === 0.44)
}
const badParse = I.parseQuoteText('v_sz999999="1~测试~999999~";')
check('字段不足优雅失败', badParse.length === 1 && badParse[0].ok === false)

console.log('\n=== 5. 时间解析与时效 ===')
const t = I.parseQuoteTime('20261019142647')
check('时间戳可解析', t !== null)
check('时间正确', new Date(t).getFullYear() === 2026 && new Date(t).getMonth() === 9)
check('非法时间返回 null', I.parseQuoteTime('abc') === null)
check('短串返回 null', I.parseQuoteTime('2026') === null)
const age = I.quoteAgeSec({ quoteTime: '20261019142647' }, new Date(2026, 9, 19, 14, 27, 47).getTime())
check('ageSec 计算正确(60s)', age === 60, String(age))

console.log('\n=== 6. 交易时段 ===')
const mk = (y, mo, d, h, mi) => new Date(y, mo - 1, d, h, mi)
// 2026-10-19 是周一
check('周一 09:00 未开盘', I.tradingSession(mk(2026,10,19,9,0)).open === false)
check('周一 10:30 上午交易', I.tradingSession(mk(2026,10,19,10,30)).phase === 'am')
check('周一 11:45 午休', I.tradingSession(mk(2026,10,19,11,45)).phase === 'noon')
check('周一 13:30 下午交易', I.tradingSession(mk(2026,10,19,13,30)).phase === 'pm')
check('周一 15:30 收盘', I.tradingSession(mk(2026,10,19,15,30)).phase === 'post')
check('周六休市', I.tradingSession(mk(2026,10,17,10,30)).open === false)

console.log('\n=== 7. 预警判定（红绿） ===')
const buy = I.evaluate({ ok: true, symbol: '600126', name: 'X', pct: -4.0, price: 5, volumeRatio: 1.0, turnover: 1.0, quoteTime: '20261019143000' })
check('跌 4% 触发买点', buy?.side === 'buy')
check('买点绿闪', buy?.flash === 'green')
const sell = I.evaluate({ ok: true, symbol: '600126', name: 'X', pct: 5.5, price: 6, volumeRatio: 3.0, turnover: 1.0, quoteTime: '20261019143000' })
check('涨 5.5% 触发卖点', sell?.side === 'sell')
check('卖点红闪', sell?.flash === 'red')
check('放量共振升三级', sell?.level === 3)
check('幅度不足无预警', I.evaluate({ ok: true, symbol: 'X', pct: 1.0, volumeRatio: 1.0, turnover: 1.0 }) === null)
const watch = I.evaluate({ ok: true, symbol: 'X', name: 'Y', pct: 1.0, volumeRatio: 4.0, turnover: 1.0 })
check('放量但幅度不足=关注级', watch?.side === 'watch' && watch?.flash === null)
check('失败行情不触发', I.evaluate({ ok: false }) === null)

console.log('\n=== 8. 真实行情抓取 ===')
const client = I.createQuoteClient()
const { quotes, error } = await client.fetch('600126,000657,512480')
check('抓取无错误', !error, error ?? '')
const good = quotes.filter(q => q.ok)
check('至少 1 条成功', good.length >= 1, `${good.length}/${quotes.length}`)
if (good.length > 0) {
  const q = good[0]
  console.log('    样本:', q.symbol, q.name, 'price=' + q.price, 'pct=' + q.pct, 'time=' + q.quoteTime)
  check('含行情时间', !!q.quoteTime)
  check('行情时间可解析', I.parseQuoteTime(q.quoteTime) !== null)
  check('价格为正', q.price > 0)
  const a = I.quoteAgeSec(q)
  console.log('    行情时间距今:', a, '秒')
}
const st = client.stats()
console.log('    统计:', JSON.stringify(st))

console.log('\n=== 9. 缓存 ===')
const before = client.stats().cacheHits
await client.fetch('600126,000657,512480')
const after = client.stats().cacheHits
check('二次请求命中缓存', after > before, `${before} -> ${after}`)

console.log('\n=== 10. 批量上限 ===')
const many = Array.from({ length: I.MAX_BATCH + 1 }, (_, i) => String(600000 + i)).join(',')
const over = await client.fetch(many)
check('超上限被拒绝', !!over.error && over.error.includes(String(I.MAX_BATCH)))

console.log('\n=== 结果 ===')
console.log(`  通过 ${ok.length} · 失败 ${bad.length}`)
if (bad.length) { console.log('  失败项:'); bad.forEach(b => console.log('    - ' + b)) }
// Use exitCode rather than exit(): calling process.exit while fetch handles are
// still closing trips a libuv assertion on Windows ("!(handle->flags &
// UV_HANDLE_CLOSING)"), which turns a green run into a crash exit code.
process.exitCode = bad.length ? 1 : 0
