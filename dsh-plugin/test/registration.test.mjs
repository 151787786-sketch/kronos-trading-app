import { apply, __internal as I } from '../dsh/index.js'

// 模拟 cordis ctx：只提供 tools.register
const registered = []
const ctx = {
  tools: {
    register(tool) {
      if (registered.some(t => t.name === tool.name)) {
        throw new Error(`duplicate tool name: ${tool.name}`)
      }
      // 校验工具形状
      if (typeof tool.name !== 'string' || !tool.name) throw new Error('tool.name missing')
      if (typeof tool.description !== 'string' || !tool.description) throw new Error('tool.description missing')
      if (typeof tool.execute !== 'function') throw new Error('tool.execute missing')
      if (!tool.parameters || tool.parameters.type !== 'object') throw new Error('tool.parameters invalid')
      registered.push(tool)
    },
  },
}

const ok = []
const bad = []
const check = (l, c, d = '') => {
  if (c) { ok.push(l); console.log('  OK ' + l) }
  else { bad.push(l); console.log('  XX ' + l + (d ? ' — ' + d : '')) }
}

console.log('=== 1. apply 注册工具 ===')
apply(ctx, {})
check('注册 3 个工具', registered.length === 3, String(registered.length))
const names = registered.map(t => t.name)
console.log('    工具:', names.join(', '))
check('含 kronos_quote', names.includes('kronos_quote'))
check('含 kronos_watch', names.includes('kronos_watch'))
check('含 kronos_status', names.includes('kronos_status'))

console.log('\n=== 2. 工具描述质量（模型据此决定是否调用） ===')
for (const t of registered) {
  check(`${t.name} 描述 >= 80 字符`, t.description.length >= 80, String(t.description.length))
  check(`${t.name} 无 undefined 泄漏`, !t.description.includes('undefined'))
}

console.log('\n=== 3. 执行 kronos_quote（真实数据） ===')
const quoteTool = registered.find(t => t.name === 'kronos_quote')
const qr = await quoteTool.execute({ symbols: '600126,000657' })
check('返回 ok', qr.ok === true)
check('含 quotes 数组', Array.isArray(qr.quotes))
check('含 session', !!qr.session)
check('声明时效依据为 field 30', String(qr.timeBasis).includes('field 30'), qr.timeBasis)
check('含 source', qr.source === 'qt.gtimg.cn')
console.log('    返回条数:', qr.quotes.length)
if (qr.quotes.length) {
  const q = qr.quotes[0]
  console.log('    样本:', q.symbol, q.name, 'price=' + q.price, 'age=' + q.ageSec + 's')
  check('每条含 ageSec', q.ageSec !== undefined)
}

console.log('\n=== 4. render 输出（模型看到的文本） ===')
const rendered = quoteTool.render({ symbols: '600126' }, qr)
check('render 返回 text 块', Array.isArray(rendered) && rendered[0].type === 'text')
const txt = rendered[0].text
console.log('---render 输出---')
console.log(txt.split('\n').slice(0, 6).join('\n'))
console.log('---')
check('含表头', txt.includes('| code |'))
check('含市场状态', /Market: (OPEN|CLOSED)/.test(txt))
check('说明时效依据', txt.includes('field 30'))
check('无 undefined', !txt.includes('undefined'))
check('无 NaN', !txt.includes('NaN'))

console.log('\n=== 5. 执行 kronos_watch ===')
const watchTool = registered.find(t => t.name === 'kronos_watch')
const wr = await watchTool.execute({ symbols: '600126,000657,512480', pctUp: 0.5, pctDown: -0.5 })
check('返回 ok', wr.ok === true)
check('回显规则', !!(wr.rules && wr.rules.pctUp === 0.5))
check('含 checked 计数', typeof wr.checked === 'number')
check('含 alerts 数组', Array.isArray(wr.alerts))
console.log('    检查', wr.checked, '只，触发', wr.alerts.length, '条')
for (const a of wr.alerts.slice(0, 3)) {
  console.log('      ', a.symbol, a.name, a.side, 'level=' + a.level, 'flash=' + a.flash)
}
// 用极低阈值确保触发
const wr2 = await watchTool.execute({ symbols: '600126', pctUp: -99, pctDown: 99 })
check('低阈值必然触发', wr2.alerts.length >= 1, String(wr2.alerts.length))
if (wr2.alerts[0]) {
  const a = wr2.alerts[0]
  check('买点绿闪 / 卖点红闪一致',
    (a.side === 'buy' && a.flash === 'green') || (a.side === 'sell' && a.flash === 'red') || a.side === 'watch')
}
const wrender = watchTool.render({}, wr)
check('watch render 无 NaN', !wrender[0].text.includes('NaN'))
check('watch render 含红绿说明', /flash|green|red/i.test(wrender[0].text))

console.log('\n=== 6. 执行 kronos_status ===')
const statusTool = registered.find(t => t.name === 'kronos_status')
const sr = await statusTool.execute({})
check('返回 ok', sr.ok === true)
check('含 session', !!sr.session)
check('含 stats', !!sr.stats)
check('含批量上限', sr.batchCap === I.MAX_BATCH)
const srender = statusTool.render({}, sr)
console.log('---status 输出---')
console.log(srender[0].text)
console.log('---')
check('status render 无 undefined', !srender[0].text.includes('undefined'))

console.log('\n=== 7. 错误路径不抛异常 ===')
const errRes = await quoteTool.execute({ symbols: '' })
check('空符号返回错误而非抛出', errRes.ok === false && !!errRes.error)
const errRender = quoteTool.render({}, errRes)
check('错误 render 可读', errRender[0].text.includes('failed'))

console.log('\n=== 8. 自定义 config ===')
const custom = []
apply({ tools: { register: t => custom.push(t) } }, {
  toolName: 'my_quote',
  watchToolName: 'my_watch',
  statusToolName: 'my_status',
})
check('可用 config 改工具名', custom.map(t => t.name).join(',') === 'my_quote,my_watch,my_status',
  custom.map(t => t.name).join(','))

console.log('\n=== 结果 ===')
console.log(`  通过 ${ok.length} · 失败 ${bad.length}`)
if (bad.length) { console.log('  失败项:'); bad.forEach(b => console.log('    - ' + b)) }
// exitCode, not exit(): process.exit() while fetch handles are still closing
// trips a libuv assertion on Windows and corrupts a green run's exit code.
process.exitCode = bad.length ? 1 : 0
