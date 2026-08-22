# Kronos — 金融 K 线基础大模型（本地部署）

Kronos 是首个开源金融 K 线（candlestick）基础模型，在 45+ 全球交易所数据上预训练，
由两层结构组成：分层离散 Tokenizer + 自回归 Transformer。

本目录是完整的本地部署，已预下载 **Kronos-base (102.3M)** 权重，开箱即用。

## 一键启动（图形界面）

双击 **`启动Kronos.bat`**，浏览器会自动打开 **http://localhost:7070**。

使用步骤（网页内操作）：
1. **选择数据文件**：下拉选择 `BTC_USDT_5min_sample.csv`（示例数据），
   或把您自己的 K 线 CSV（需含 `open/high/low/close` 列，可选 `volume/amount/timestamps`）放入 `data\` 目录
2. **加载模型**：选择 Kronos-base 和计算设备（CUDA 优先，CPU 兜底）
3. **设置参数**：温度 T（推荐 1.2–1.5）、top_p（推荐 0.95–1.0）、采样数（推荐 2–3）
4. **选择时间窗**：滑块选择 400+120 根 K 线范围
5. **开始预测**：查看预测 K 线与实际对比

停止服务：在启动窗口按 `Ctrl+C`。

## 命令行预测

```bat
.venv\Scripts\python.exe examples\prediction_example.py
```

或修改 `examples\prediction_example.py` 中的模型名/数据路径后运行。

## 环境信息

- Python 3.12.8 虚拟环境：`.venv`
- torch 2.13.0+cu126（CUDA，RTX 3060 实测单次 120 点预测约 5–6 秒）
- 模型权重（本地，无需联网下载）：
  - `models\Kronos-base\` — 102.3M 参数主模型
  - `models\Kronos-Tokenizer-base\` — 配套 Tokenizer
- 示例数据：`data\BTC_USDT_5min_sample.csv`（合成 BTC 5 分钟 K 线，800 根）

## 换用其他模型（Kronos-small / Kronos-mini）

网页里下拉选择即可。若对应权重未预下载，程序会从 Hugging Face 下载
（自动走 `https://hf-mirror.com` 镜像；直连不通时请确认网络）。

## 常见问题

- **端口 7070 被占用**：修改 `webui\launch.py` 中的 `port=7070`。
- **模型加载失败**：确认 `models\` 下存在 `Kronos-base` 和 `Kronos-Tokenizer-base` 两个目录及 `config.json`/`model.safetensors`。
- **GPU 显存不足**：网页设备下拉选择 CPU（Kronos-base 仅 102M 参数，6GB 显存绰绰有余）。
- **首次预测慢**：模型首次加载需几秒，属正常现象。

## 参考

- 官方仓库：https://github.com/shiyu-coder/Kronos
- 论文：https://arxiv.org/abs/2508.02739 （AAAI 2026）
- 模型权重：https://huggingface.co/NeoQuasar/Kronos-base
