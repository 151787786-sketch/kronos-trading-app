"""Round 7: 新增外部数据源 + DeepSeek 全量接入 + 夜间自迭代 的回归测试。

运行前需先启动 APP（python app.py）。
"""
import json
import re
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:7071"
PASS = FAIL = 0
FAILURES = []


def req(method, path, body=None, timeout=60):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:300]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}
    except Exception as e:
        return -1, {"error": str(e)}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  XX {name} {detail}")


def _first_hex(text, var):
    """取某个 CSS 变量第一次出现的 hex 值。"""
    m = re.search(re.escape(var) + r":\s*(#[0-9a-fA-F]{6})", text)
    return m.group(1) if m else ""


def _rgb(hx):
    hx = (hx or "").lstrip("#")
    if len(hx) != 6:
        return (0, 0, 0)
    return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))


def _is_red(hx):
    """语义判定：R 明显高于 G/B 即为红（不锁死具体色号，浅色/深色主题都能过）"""
    r, g, b = _rgb(hx)
    return r > g + 40 and r > b + 40


def _is_green(hx):
    r, g, b = _rgb(hx)
    return g > r + 40 and g > b + 40


print("=== 1. DeepSeek 接入层 ===")
st, s = req("GET", "/api/llm/status")
check("状态接口 200", st == 200)
check("返回 available 字段", "available" in s)
check("密钥打码不外泄", "***" in (s.get("key_masked") or "") or s.get("key_masked") == "")
check("统计字段齐全", all(k in s for k in ("calls", "ok", "fail", "cache_hits", "prompt_tokens")))

if s.get("available"):
    st, t = req("POST", "/api/llm/test", {}, timeout=120)
    err = str(t.get("error") or "")
    net_down = any(k in err for k in ("SSLError", "ConnectionError", "Max retries",
                                      "Timeout", "timed out", "Temporary failure",
                                      "EOF occurred", "NewConnectionError"))
    if net_down and not t.get("ok"):
        # 网络不通（代理抖动/被墙）≠ 集成坏了：明确跳过，避免把环境问题报成缺陷
        print(f"  -- DeepSeek 网络暂不可达，跳过连通性断言（{err[:80]}）")
    else:
        check("连通性测试成功", st == 200 and t.get("ok") is True, str(t)[:160])
        check("测试返回内容非空", bool((t.get("reply") or "").strip()))
else:
    print("  -- DeepSeek 未配置，跳过连通性测试")

st, c = req("GET", "/api/llm/config")
check("配置接口 200", st == 200)
check("配置接口不返回明文密钥", "api_key" not in c)

print("\n=== 2. 宏观 / 利率数据源 ===")
st, m = req("GET", "/api/macro")
check("宏观接口 200", st == 200)
check("含通胀数据 CPI/PPI", bool(m.get("inflation", {}).get("cpi")) and bool(m.get("inflation", {}).get("ppi")))
check("含经济数据 PMI/GDP", bool(m.get("economy", {}).get("pmi")) and bool(m.get("economy", {}).get("gdp")))
check("含存款准备金率", bool(m.get("economy", {}).get("reserve")))
check("含资金利率 GC001", any("GC001" in r.get("label", "") for r in (m.get("rates", {}).get("rows") or [])))
check("宏观打分区间合法", -3 <= (m.get("score") or 0) <= 3)
check("倾向取值合法", m.get("tone") in ("偏多", "中性", "偏空"))
check("卡片非空", len(m.get("cards") or []) >= 5)

print("\n=== 3. 行业景气度 ===")
st, rk = req("GET", "/api/industry/ranking?limit=10")
check("行业榜接口 200", st == 200)
rows = rk.get("ranking") or []
check("返回行业条目", len(rows) >= 5)
check("每条含景气度评分", all("prosperity" in x for x in rows))
check("景气度在 0~100", all(0 <= x["prosperity"] <= 100 for x in rows))
check("按景气度降序", all(rows[i]["prosperity"] >= rows[i + 1]["prosperity"] for i in range(len(rows) - 1)))
check("含领涨股字段", all("leader" in x for x in rows))

st, one = req("GET", "/api/industry/600519")
check("个股行业接口 200", st == 200)
check("识别出行业", bool(one.get("industry")))
check("匹配到板块数据", bool(one.get("board")))

st, bad = req("GET", "/api/industry/000000")
check("无效代码不报 500", st == 200)

print("\n=== 4. 舆情情感分析 ===")
st, se = req("GET", "/api/sentiment/600519")
check("舆情接口 200", st == 200)
check("返回样本数", (se.get("count") or 0) > 0)
check("情感均值在 -1~1", -1 <= (se.get("avg") or 0) <= 1)
check("倾向标签存在", bool(se.get("label")))
check("风险等级合法", se.get("risk_level") in ("低", "中", "高"))
check("正负中数量之和等于样本数", (se.get("pos", 0) + se.get("neg", 0) + se.get("neutral", 0)) == se.get("count"))
check("条目带情感分", all("final" in it for it in (se.get("items") or [])))
check("条目情感分在 -1~1", all(-1 <= it["final"] <= 1 for it in (se.get("items") or [])))

st, se2 = req("GET", "/api/sentiment/600519?llm=0")
check("可强制使用词典引擎", st == 200 and se2.get("engine") == "lexicon")

st, b = req("POST", "/api/sentiment/brief", {"symbol": "600519"}, timeout=180)
check("舆情 AI 解读接口 200", st == 200)
check("解读返回结构完整", "ok" in b and "result" in b)

st, e = req("GET", "/api/sentiment/999999")
check("无效标的返回 200", st == 200)

print("\n=== 5. 券商研报数据库 ===")
st, rp = req("GET", "/api/reports/600519?limit=10")
check("研报接口 200", st == 200)
check("返回研报列表", len(rp.get("reports") or []) > 0)
first = (rp.get("reports") or [{}])[0]
check("研报含机构与日期", bool(first.get("org")) and bool(first.get("date")))
check("评级统计存在", "total" in (rp.get("ratings") or {}))
check("评级分布非空", bool((rp.get("ratings") or {}).get("dist")))

st, ri = req("GET", "/api/reports/industry?days=90")
check("行业研报接口 200", st == 200)
check("返回行业研报", len(ri.get("reports") or []) > 0)
check("返回策略报告", "strategy" in ri)

print("\n=== 6. 夜间自迭代 ===")
st, np_ = req("GET", "/api/nightly/params")
check("参数接口 200", st == 200)
params = np_.get("params") or []
check("参数数量 >= 10", len(params) >= 10)
check("每个参数含范围", all("min" in p and "max" in p and "default" in p for p in params))
check("当前值在允许范围内", all(p["min"] <= p["current"] <= p["max"] for p in params))

st, its = req("GET", "/api/nightly/iterations?limit=5")
check("迭代列表 200", st == 200 and "iterations" in its)

st, run = req("POST", "/api/nightly/run", {"dry_run": True}, timeout=900)
check("演练迭代 200", st == 200)
check("演练返回 run id", bool(run.get("id")))
check("含证据", bool((run.get("evidence") or {}).get("items")))
check("含建议列表", isinstance(run.get("proposals"), list))
check("演练标记正确", run.get("dry_run") is True)
# 演练必须不写入 tuning.json：生效时间不应因演练而更新
st_before, p_before = req("GET", "/api/nightly/params")
check("演练未改动生效参数",
      (p_before.get("last_iteration") or 0) != run["id"],
      f"last_iteration={p_before.get('last_iteration')} run={run['id']}")

st, det = req("GET", f"/api/nightly/iterations/{run['id']}")
check("迭代详情 200", st == 200 and det.get("id") == run["id"])

# 生效类参数不会被越界改动
for p in params:
    if p["changed"]:
        check(f"参数 {p['param']} 处于允许范围", p["min"] <= p["current"] <= p["max"])

print("\n=== 7. 买卖点 AI 解读 ===")
st, br = req("POST", "/api/buysell/reading", {"symbol": "600519", "forecast": False}, timeout=300)
check("解读接口 200", st == 200)
check("返回买卖点计划", bool(br.get("plan")))
plan = br.get("plan") or {}
check("计划含止损与目标", plan.get("stop_loss") is not None and plan.get("target_up") is not None)
check("止损低于目标", (plan.get("stop_loss") or 0) < (plan.get("target_up") or 0))
st, br2 = req("POST", "/api/buysell/reading", {}, timeout=60)
check("缺参返回 400", st == 400)

print("\n=== 8. 需求对照表 ===")
st, rq = req("GET", "/api/research/requirements")
check("对照表接口 200", st == 200)
s = rq.get("summary") or {}
check("统计字段完整", all(k in s for k in ("total", "done", "partial", "todo", "done_pct")))
check("条目总数等于各模块之和",
      sum(len(m["items"]) for m in rq["modules"]) == s["total"])
check("状态取值合法",
      all(it["status"] in ("done", "partial", "todo") for m in rq["modules"] for it in m["items"]))
check("每条都有网页可见位置", all(it.get("ui") for m in rq["modules"] for it in m["items"] if it["status"] != "todo"))
check("完成率与计数一致", abs(s["done_pct"] - round(s["done"] / s["total"] * 100, 1)) < 0.11)

print("\n=== 9. 涨跌配色（A股红涨绿跌） ===")
import re as _re
try:
    with urllib.request.urlopen(urllib.request.Request(BASE + "/"), timeout=30) as resp:
        page = resp.read().decode("utf-8", "replace")
    st = resp.status
except Exception as e:
    page, st = "", -1
check("首页 200", st == 200 and len(page) > 10000, f"len={len(page)}")

check("默认配色方案为 A股(cn)", 'data-scheme="cn"' in page or "applyScheme('cn')" in page)
check("定义 --up / --down 变量", "--up:" in page and "--down:" in page)
check("默认 --up 为红色（语义判定，不锁死色号）",
      _is_red(_first_hex(page, "--up")), _first_hex(page, "--up"))
check("默认 --down 为绿色（语义判定）",
      _is_green(_first_hex(page, "--down")), _first_hex(page, "--down"))
check(".pos 使用 --up", bool(_re.search(r"\.pos\s*\{\s*color:\s*var\(--up\)", page)))
check(".neg 使用 --down", bool(_re.search(r"\.neg\s*\{\s*color:\s*var\(--down\)", page)))
check("提供欧美配色覆盖块", 'html[data-scheme="western"]' in page)
check("欧美覆盖块存在（绿涨红跌可切）", bool(_re.search(r'data-scheme="western"\]', page)))
check("顶栏有配色切换按钮", 'id="scheme-toggle"' in page)
check("方向类颜色不再硬编码 green/red",
      "o.side==='BUY'?'var(--up)':'var(--down)'" in page)
check("语义色 tone-good/tone-bad 独立存在", ".tone-good" in page and ".tone-bad" in page)
check("K线涨跌色跟随配色", "schemeColor('up')" in page and "schemeColor('down')" in page)
check("宏观卡片用语义色而非涨跌色", "c.tone==='good'?'tone-good'" in page)
check("风险等级用语义色而非涨跌色", "i.level==='高'?'tone-bad'" in page)

print("\n=== 10. 背景系统（AeroShards + Spline） ===")
check("碎片画布存在", 'id="aero-bg"' in page)
check("Spline 背景层存在", 'id="spline-bg"' in page)
check("背景设置面板存在", 'id="bg-panel"' in page)
check("背景按钮存在", 'id="fx-toggle"' in page)
check("引入背景管理器", '/static/background.js' in page)
check("引入碎片引擎", '/static/aero-shards.js' in page)
check("AeroShards 参数与原组件一致",
      all(k in page for k in ["shardColor: '#896ABD'", "accentColor: '#A855F7'",
                              "backgroundColor: '#120F17'", "holdToGather: true",
                              "chromaticAberration: 0.0075", "edgeSoftness: 2"]))
check("背景层不抢交互（pointer-events:none）", '#spline-bg { position: fixed' in page and 'pointer-events: none' in page)

for asset, label in [("/static/background.js", "背景管理器"),
                     ("/static/spline-scene.js", "SplineScene 适配层"),
                     ("/static/vendor/spline-runtime.js", "Spline 运行时(本地)"),
                     ("/static/aero-shards.js", "碎片引擎")]:
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + asset), timeout=30) as resp:
            body = resp.read()
        check(f"{label} 可访问且非空", resp.status == 200 and len(body) > 500, f"{len(body)} bytes")
    except Exception as e:
        check(f"{label} 可访问且非空", False, str(e)[:80])

# Spline 适配层必须与用户给的组件保持同名同参数
try:
    with urllib.request.urlopen(urllib.request.Request(BASE + "/static/spline-scene.js"), timeout=30) as resp:
        sc = resp.read().decode("utf-8", "replace")
    check("适配层导出 SplineScene", "export { SplineScene }" in sc)
    check("适配层保留 scene 属性", "scene" in sc and "className" in sc)
    check("适配层有 loader 兜底（对应 Suspense fallback）", "spline-fallback" in sc and "loader" in sc)
    check("适配层使用本地运行时（不依赖 CDN）", "./vendor/spline-runtime.js" in sc)
except Exception as e:
    check("适配层可读取", False, str(e)[:80])

print("\n=== 11. Gateway Flow 流线背景 ===")
check("流线画布存在", 'id="flow-bg"' in page)
check("引入流线引擎", '/static/gateway-flow.js' in page)
check("五个背景模式按钮齐全",
      all(f'id="bg-mode-{m}"' in page for m in ("galaxy", "cyber", "flow", "shards", "spline")))
check("面板为纸面 + 虚线描边（TypeSafe 语言）", '.panel { background: var(--panel); border: 1px dashed' in page)
check("红涨绿跌语义未被主题破坏（涨=红 / 跌=绿）",
      _is_red(_first_hex(page, "--up")) and _is_green(_first_hex(page, "--down")),
      "%s / %s" % (_first_hex(page, "--up"), _first_hex(page, "--down")))

try:
    with urllib.request.urlopen(urllib.request.Request(BASE + "/static/gateway-flow.js"), timeout=30) as resp:
        gf = resp.read().decode("utf-8", "replace")
    check("流线引擎可访问且非空", len(gf) > 3000, f"{len(gf)} bytes")
    check("保留原版 80 条流线基数", "BASE_PATHS = 80" in gf)
    check("保留原版虚线样式 [1,4]", "BASE_DASH = [1, 4]" in gf)
    check("保留原版线宽 1.2", "BASE_LINE_WIDTH = 1.2" in gf)
    check("保留点击爆破交互", "explosions" in gf and "addEventListener('click'" in gf)
    check("保留贝塞尔汇聚中心", "bezierCurveTo" in gf and "centerX" in gf)
    check("参数与原组件同名",
          all(k in gf for k in ("density", "strokeWidth", "opacity",
                                "hue", "saturation", "brightness")))
    check("零外部依赖（无 CDN / React / Tailwind）",
          "http://" not in gf and "https://" not in gf and "import " not in gf)
except Exception as e:
    check("流线引擎可读取", False, str(e)[:80])

print("\n=== 12. 赛博终端主题（可切换） ===")
m = re.search(r'html\[data-theme="cyber"\]\s*\{(.*?)\n\}', page, re.S)
dark = m.group(1) if m else ""
check("存在赛博主题覆盖块 html[data-theme=cyber]", bool(dark))
check("赛博底纯黑", "--bg: #000000" in dark)
check("赛博强调霓虹绿 #00ff41", "--accent: #00ff41" in dark)
check("赛博正文骨白 #d7e3db", "--text: #d7e3db" in dark)
check("赛博玻璃面板", "--panel: hsla(0, 0%, 4%, .62)" in dark)
check("赛博涨跌为亮红/亮绿",
      _is_red(_first_hex(dark, "--up")) and _is_green(_first_hex(dark, "--down")),
      "%s / %s" % (_first_hex(dark, "--up"), _first_hex(dark, "--down")))

# 赛博签名元素（与主题无关，始终存在）
check("绿色网格衬底 48px", 'id="substrate"' in page and 'background-size: 48px 48px' in page)
check("胶噪层 SVG feTurbulence", 'id="noise"' in page and 'feTurbulence' in page)
check("扫描线层 mix-blend-mode:overlay",
      'id="scanlines"' in page and 'mix-blend-mode: overlay' in page)
check("暗角层 vignette", 'id="vignette"' in page and 'rgba(0,0,0,.7) 100%' in page)
check("霓虹辉光三件套 glow-bone/retina/electro",
      '.glow-bone' in page and '.glow-retina' in page and '.glow-electro' in page)
check("辉光用 text-shadow 双层", 'text-shadow: 0 0 6px rgba(' in page)
check("故障字 glitch（双向偏移 + clip-path）",
      'data-text="KRONOS"' in page and 'mix-blend-mode: screen' in page and 'clip-path: polygon' in page)
check("闪烁光标 blinkCursor 关键帧", '@keyframes blinkCursor' in page)
check("纸面虚线网格（呼应虚线描边语言）",
      'id="paper-bg"' in page and 'background-size: 72px 72px' in page)

print("\n=== 13. Galaxy 银河背景（ReactBits 移植） ===")
check("银河画布存在", 'id="galaxy-bg"' in page)
check("引入银河引擎", '/static/galaxy.js' in page)
# 用户给定的 props 必须原样落地
check("density = 1.5", 'density: 1.5' in page)
check("glowIntensity = 0.5", 'glowIntensity: 0.5' in page)
check("saturation = 0.8", 'saturation: 0.8' in page)
check("hueShift = 240", 'hueShift: 240' in page)
check("mouseRepulsion = true", 'mouseRepulsion: true' in page)
check("mouseInteraction = true", 'mouseInteraction: true' in page)

try:
    with urllib.request.urlopen(urllib.request.Request(BASE + "/static/galaxy.js"), timeout=30) as resp:
        gx = resp.read().decode("utf-8", "replace")
    check("银河引擎可访问且非空", len(gx) > 6000, f"{len(gx)} bytes")
    # 原版 shader 关键常量
    check("保留 NUM_LAYER 4 层", "#define NUM_LAYER 4.0" in gx)
    check("保留 Star 光芒函数", "float Star(vec2 uv, float flare)" in gx)
    check("保留 StarLayer 网格采样（3×3）", "vec3 StarLayer(vec2 uv)" in gx and "int y = -1; y <= 1" in gx)
    check("保留 Hash21 噪声", "float Hash21(vec2 p)" in gx)
    check("保留 hsv2rgb 色相旋转", "vec3 hsv2rgb(vec3 c)" in gx)
    check("保留 MAT45 射线矩阵", "#define MAT45 mat2(0.7071" in gx)
    check("保留鼠标排斥分支", "uMouseRepulsion > 0.5" in gx and "uRepulsionStrength" in gx)
    check("保留鼠标位移分支（非排斥模式）", "mouseOffset" in gx)
    check("保留自适应旋转 autoRot", "autoRot" in gx)
    check("保留闪烁 twinkle", "uTwinkleIntensity" in gx)
    # 原版全部 props 默认值
    check("原版默认值齐全（hueShift 140 / starSpeed 0.5 / speed 1 / repulsionStrength 2）",
          "hueShift: 140" in gx and "starSpeed: 0.5" in gx and "speed: 1.0" in gx
          and "repulsionStrength: 2" in gx)
    check("保留透明模式 alpha 输出", "uTransparent > 0.5" in gx and "smoothstep(0.0, 0.3, alpha)" in gx)
    check("保留浅色模式分支", "uLightMode > 0.5" in gx)
    # 移植修正
    check("修复 GLSL ES 1.0 浮点循环问题（改 int 循环）",
          "for (int li = 0; li < 4; li++)" in gx)
    check("零第三方依赖（不含 ogl / import）",
          "from 'ogl'" not in gx and "import " not in gx and "require(" not in gx)
    check("本地自包含（无 CDN 引用）", "http://" not in gx and "https://" not in gx)
except Exception as e:
    check("银河引擎可读取", False, str(e)[:80])

print("\n=== 14. 界面缩放（字号 1.5 倍） ===")
check("html 使用 zoom 缩放变量", 'html { zoom: var(--ui-scale, 1.5); }' in page)
check("默认缩放 1.5", "SCALE_DEFAULT = 1.5" in page)
check("顶栏有缩放控件", 'id="scale-label"' in page and "stepScale(-0.1)" in page and "stepScale(0.1)" in page)
check("缩放值持久化到 localStorage", "kronos_ui_scale" in page)
check("支持 ?scale= 直达参数", "location.search" in page and "parseFloat(q)" in page)
check("缩放后通知背景层重新适配", "window[k].resize" in page or "typeof window[k].resize" in page)
# 放大后不能把整页撑破：这几条是防止布局溢出的关键守卫
check("网格列用 minmax(0,1fr) 而非 1fr", "grid-template-columns: 300px minmax(0, 1fr)" in page)
check("网格子项 min-width:0（否则宽表格撑破整页）", ".wrap > * { min-width: 0; }" in page)
check("顶栏允许换行（放大后不溢出）", "flex-wrap: wrap" in page)
check("面板内表格可横向滚动", ".panel { overflow-x: auto; }" in page)
check("长文本表格允许换行", ".wrap-cells th, .wrap-cells td { white-space: normal" in page)
check("需求对照表已用 wrap-cells", 'class="wrap-cells"' in page)

print("\n=== 15. 科技风主题（默认，静态） ===")
check("默认主题为 tech", 'data-theme="tech"' in page or "return 'tech'" in page)
check("深空蓝黑底 #070b14", '--bg: #070b14' in page)
check("青色主强调 #22d3ee", '--accent: #22d3ee' in page)
check("蓝色次强调 #3b82f6", '--accent-2: #3b82f6' in page)
check("浅灰蓝正文 #cbd8ec", '--text: #cbd8ec' in page)
check("青色细线边框", '--border: rgba(56, 189, 248, .18)' in page)
check("科技网格背景层", 'id="tech-bg"' in page and 'background-size: 40px 40px' in page)
check("直角（科技风保持直角）", 'border-radius: 0' in page)
check("深色底涨跌用亮红亮绿",
      _is_red(_first_hex(page, "--up")) and _is_green(_first_hex(page, "--down")),
      "%s / %s" % (_first_hex(page, "--up"), _first_hex(page, "--down")))
check("三套主题都可切换", all(t in page for t in ("'tech'", "'light'", "'cyber'")))
check("顶栏有主题切换按钮", 'id="theme-toggle"' in page and 'toggleTheme()' in page)
check("图表配色跟随主题（Plotly 用具体色值）",
      'function chartBg()' in page and 'function chartGrid()' in page and 'function chartFont()' in page)
check("默认背景为科技网格",
      "mode: 'tech'" in __import__("urllib.request", fromlist=["x"]).urlopen(
          BASE + "/static/background.js", timeout=30).read().decode("utf-8", "replace"))

print("\n=== 16. 去动效（默认关闭全部动画） ===")
check("html 带 data-motion 属性（首屏前设定）", 'data-motion' in page)
check("默认动效关闭", "var t = 'tech', m = 'off'" in page or 'kronos_motion' in page)
check("顶栏有动效开关", 'id="motion-toggle"' in page and 'toggleMotion()' in page)
check("全局关闭 animation / transition",
      'animation: none !important' in page and 'transition: none !important' in page)
check("作用于 ::before/::after 伪元素",
      'html[data-motion="off"] *::before' in page and 'html[data-motion="off"] *::after' in page)
check("尊重系统 prefers-reduced-motion",
      'prefers-reduced-motion: reduce' in page)
check("动效关闭时不启动画布渲染循环",
      "data-motion') === 'off'" in __import__("urllib.request", fromlist=["x"]).urlopen(
          BASE + "/static/background.js", timeout=30).read().decode("utf-8", "replace"))
check("支持 ?motion=on|off", "get('motion')" in page)

print("\n=== 17. 浅色主题（TypeSafe，可切换） ===")
lm = re.search(r'html\[data-theme="light"\]\s*\{(.*?)\n\}', page, re.S)
light = lm.group(1) if lm else ""
check("存在浅色主题覆盖块", bool(light))
check("纸面白 #fefefe", '--bg: #fefefe' in light)
check("近黑文字 #1e1e1e", '--text: #1e1e1e' in light)
check("品红强调 #d45bb6（原站 ::selection 色）", '--accent: #d45bb6' in light)
check("浅色涨跌用加深版",
      _is_red(_first_hex(light, "--up")) and _is_green(_first_hex(light, "--down")),
      "%s / %s" % (_first_hex(light, "--up"), _first_hex(light, "--down")))
check("::selection 使用主题强调色", '::selection { background: var(--magenta)' in page)

print("\n" + "=" * 50)
print(f"RESULT: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("FAILED: " + ", ".join(FAILURES))
sys.exit(1 if FAIL else 0)
