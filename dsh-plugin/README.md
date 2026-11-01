# dsh-kronos-watch

DeepSeek Harness (DSH) plugin — A-share realtime watch, backed by the free Tencent quote API.

在 DSH 对话里直接查 A 股行情、扫买卖点预警、看盘口状态，不用切浏览器。

## 安装

```bash
dsh plugin --profile web add dsh-kronos-watch
```

装好后重启 DSH，工具会自动出现在工具列表里。

## 三个工具

| 工具 | 用途 |
|---|---|
| `kronos_quote` | 批量实时行情：价格/涨跌/高低/量比/换手/行情时间 |
| `kronos_watch` | 按阈值扫买卖点，返回红绿频闪建议 |
| `kronos_status` | 市场时段、请求统计、缓存状态 |

### 使用示例

直接跟 DSH 说：

- 「查一下 600519 和 000001 现在多少钱」
- 「看看我的自选股有没有需要关注的」
- 「A 股现在开盘吗」

模型会自动调用对应工具。

## 设计要点

**数据源**
- `https://qt.gtimg.cn/q=` 批量接口，一次最多 500 只
- 返回 gb18030 编码，`~` 分隔共 88 字段
- **行情时效以第 30 字段（交易所时间戳 `YYYYMMDDHHMMSS`）为唯一依据**
  —— 不使用任何上游的 `update_time` 或本地时钟

**限流**
- 请求起点间隔 ≥ 500ms（跨调用共享，不会并发轰炸）
- 行情缓存 5 秒（思考循环里重复调用不会重复打接口）
- 单批上限 500 只，超出直接拒绝

**红绿语义（A 股惯例）**
- **跌到位 → 买点 → 绿闪**（`flash: "green"`）
- **涨到位 → 卖点 → 红闪**（`flash: "red"`）
- 放量但幅度不够 → 关注级（`side: "watch"`，不闪）

**默认阈值**（每次调用可覆盖）

| 参数 | 默认 | 含义 |
|---|---|---|
| `pctUp` | 3.0 | 涨到多少算卖点 |
| `pctDown` | -3.0 | 跌到多少算买点 |
| `volumeRatio` | 2.5 | 量比确认 |
| `turnover` | 5.0 | 换手率确认（%） |

## 配置

在 profile 的 `cordis.patch.yml` 里可以改工具名或限流参数：

```yaml
- id: kronos-watch
  name: dsh-kronos-watch
  config:
    toolName: kronos_quote
    watchToolName: kronos_watch
    statusToolName: kronos_status
    intervalMs: 500      # 请求起点最小间隔
    cacheTtlMs: 5000     # 行情缓存时长
    timeoutMs: 12000     # 单次请求超时
```

## 依赖

无运行时依赖，只用 Node 内置模块（`fetch`、`TextDecoder`、`Intl`）。需要 Node ≥ 20。

## 已知限制

- **仅 A 股**（沪 / 深 / 北交所）。港股美股请用其他数据源。
- **非交易时段行情会停留在上一个交易时段的收盘时间**，这是数据源行为不是缓存问题。`kronos_status` 会告诉你当前是否在交易时段。
- **不提供 Level-2 数据**（逐笔委托、盘口挂单）。量比和换手是 1 分钟级聚合值。
- 本插件只读行情，**不下单、不构成投资建议**。

## 测试

```bash
node test/plugin.test.mjs        # 52 项：解析 / 前缀 / 时效 / 时段 / 红绿判定 / 真实抓取
node test/registration.test.mjs  # 38 项：工具注册 / 执行 / 渲染 / 错误路径
```

> 测试使用 `process.exitCode` 而非 `process.exit()`：后者在 Windows 上于 fetch 句柄尚未关闭时调用会触发 libuv 断言，把通过的测试变成崩溃退出码。

## License

MIT
