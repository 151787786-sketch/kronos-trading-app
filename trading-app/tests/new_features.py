"""Round 7: 新增外部数据源 + DeepSeek 全量接入 + 夜间自迭代 的回归测试。

运行前需先启动 APP（python app.py）。
"""
import json
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
check("默认 --up 为红色", bool(_re.search(r"--up:\s*#ef4444", page)), "期望上涨=红")
check("默认 --down 为绿色", bool(_re.search(r"--down:\s*#22c55e", page)), "期望下跌=绿")
check(".pos 使用 --up", bool(_re.search(r"\.pos\s*\{\s*color:\s*var\(--up\)", page)))
check(".neg 使用 --down", bool(_re.search(r"\.neg\s*\{\s*color:\s*var\(--down\)", page)))
check("提供欧美配色覆盖块", 'html[data-scheme="western"]' in page)
check("欧美覆盖块里 --up 是绿色",
      bool(_re.search(r'data-scheme="western"\]\s*\{[^}]*--up:\s*#22c55e', page, _re.S)))
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

print("\n=== 11. Gateway Flow 流线背景（默认） ===")
check("流线画布存在", 'id="flow-bg"' in page)
check("抖动网点存在（原版 dither overlay）", 'id="dither"' in page)
check("引入流线引擎", '/static/gateway-flow.js' in page)
check("默认背景为流线（background.js 里 mode 默认 flow）",
      "mode: 'flow'" in __import__("urllib.request", fromlist=["x"]).urlopen(
          BASE + "/static/background.js", timeout=30).read().decode("utf-8", "replace"))
check("三个背景模式按钮齐全",
      all(f'id="bg-mode-{m}"' in page for m in ("flow", "shards", "spline")))
check("主题为 Gateway 黑白色板",
      '--bg: #000000' in page and '--text: #cbd5e1' in page and '--muted: #64748b' in page)
check("面板保留玻璃模糊", 'backdrop-filter: blur(12px)' in page)
check("签名细节：hover 渐变扫光边框", 'mask-composite: exclude' in page)
check("涨跌色未被主题改动（仍红涨绿跌）",
      bool(_re.search(r"--up:\s*#ef4444", page)) and bool(_re.search(r"--down:\s*#22c55e", page)))

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

print("\n" + "=" * 50)
print(f"RESULT: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("FAILED: " + ", ".join(FAILURES))
sys.exit(1 if FAIL else 0)
