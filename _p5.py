import io, re

p = r"D:\a\kronos\README.md"
src = io.open(p, encoding="utf-8").read()

# 1) 功能全景表：界面主题 + 背景动画
src = re.sub(
    r"\| 🎨 \*\*界面主题\*\* \|[^\n]*\n",
    "| 🎨 **界面主题** | **默认浅色（TypeSafe AI 风格，取自 [typesafe.ai](https://typesafe.ai/)）**：纸面白 `#fefefe` + 近黑 `#1e1e1e` 文字 + 霓虹品红强调 `#d45bb6`/`#ff52fc` + **虚线描边** + **直角** + 紧凑大字号。顶栏一键切深色（保留原赛博终端配色），选择记在浏览器里 |\n",
    src, count=1)

# 2) 背景动画行：加入纸面
src = src.replace(
    "| ✨ **背景动画** | 五种模式，顶栏「🎨 背景」切换：<br>① **银河**（默认，WebGL，**零依赖**）",
    "| ✨ **背景动画** | 六种模式，顶栏「🎨 背景」切换：<br>① **纸面**（浅色主题默认，纯 CSS）——`#fefefe` 底 + 品红径向辉光 + 72px 虚线网格<br>② **银河**（WebGL，**零依赖**）")

# 3) 主题章节整体替换
start = src.find("### 主题色板（取自 [me.dufengyun.xyz]")
if start < 0:
    start = src.find("### 主题色板")
end = src.find("### 背景模式一：银河 Galaxy（默认）")

new_section = """### 主题：浅色（默认）/ 深色（可切换）

**默认 = TypeSafe AI 风格**（色板从 [typesafe.ai](https://typesafe.ai/) 的 Framer 内联样式里提取）：

| 变量 | 值 | 来源 |
|---|---|---|
| `--paper` | `#fefefe` | 原站 38.1% 像素占比的主背景色 |
| `--ink` | `#1e1e1e` | 原站 `--framer-text-color` |
| `--paper-3` | `#dedede` | 原站 `rgb(222,222,222)` |
| `--muted-paper` | `#c4c4c4` | 原站 `rgb(196,196,196)` |
| `--line` | `rgba(136,136,136,.32)` | 原站 `--border-color:#8883`（**虚线**） |
| `--magenta` | `#d45bb6` | 原站 `::selection` 背景色 |
| `--pink` | `#f386a1` | 原站 `rgb(243,134,161)` |
| `--neon` | `#ff52fc` | 原站 hero 霓虹品红 |
| `--teal` | `#09aea1` | 原站点缀青 |

原站签名元素：
- **虚线描边**（`border-style: dashed`）—— 面板、按钮、输入框、徽章全部改成虚线
- **直角**（`border-radius: 0`；原站圆角声明只有 `0px` / `4px`）
- **`::selection` 品红**（原站 `--selection-background-color`）
- **紧凑大字号**：原站标题用到 `140px`、`line-height: 80%`、`text-stroke: 1.2px`
- **等宽标签**：原站用 Fragment Mono / JetBrains Mono 做小标签

> 字体说明：原站用 Host Grotesk（Framer 私有托管），本机没有也不引入 CDN，回退到
> `Inter → HarmonyOS Sans SC → 微软雅黑` 系统栈；等宽部分用 JetBrains Mono / 系统 mono。

**深色主题**（顶栏 `🌙 深色` 切换）保留原来的赛博终端配色（`#000` 底 + `#00ff41` 霓虹绿 +
`#d7e3db` 骨白），背景默认联动切到银河。两套主题共用同一批语义变量，切换是零成本的。

> 涨跌配色在**两套主题里都用各自的加深/提亮版本**，保证对比度：
> 浅色 `#d92b1f` 红 / `#0f8a4d` 绿，深色 `#ef4444` 红 / `#22c55e` 绿。
> 测试改成**语义判定**（红必须 R 明显高于 G/B，绿反之），不再锁死具体色号。

"""
src = src[:start] + new_section + src[end:]

# 4) 背景模式编号顺延 + 纸面作为模式一
src = src.replace("### 背景模式一：银河 Galaxy（默认）", "### 背景模式二：银河 Galaxy")
src = src.replace("### 背景模式二：赛博网格", "### 背景模式三：赛博网格")
src = src.replace("### 背景模式三：流线", "### 背景模式四：流线")
src = src.replace("### 背景模式四：碎片", "### 背景模式五：碎片")
src = src.replace("### 背景模式五：Spline 3D 场景", "### 背景模式六：Spline 3D 场景")
src = src.replace(
    "### 背景模式二：银河 Galaxy\n",
    "### 背景模式一：纸面（浅色主题默认）\n纯 CSS：`#fefefe` 底 + 左上角品红径向辉光 + 72px 虚线网格（呼应虚线描边语言）。零 JS、零依赖、零网络。\n\n### 背景模式二：银河 Galaxy\n", 1)
src = src.replace("- 顶栏「🎨 背景」→ 选 银河 / 赛博网格 / 流线 / 碎片 / Spline，选择存在浏览器里",
                  "- 顶栏「🎨 背景」→ 选 纸面 / 银河 / 赛博网格 / 流线 / 碎片 / Spline，选择存在浏览器里")
src = src.replace("- URL 直达：`?bg=galaxy`", "- URL 直达：`?bg=paper` / `?bg=galaxy`")
src = src.replace("- 配色方案：`?scheme=cn`",
                  "- 主题：`?theme=light`（默认）/ `?theme=dark`\n- 配色方案：`?scheme=cn`")

io.open(p, "w", encoding="utf-8").write(src)
print("README updated")
