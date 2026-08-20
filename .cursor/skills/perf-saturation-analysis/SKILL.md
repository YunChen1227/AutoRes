---
name: perf-saturation-analysis
description: >-
  Analyze AutoRes LLM serving benchmarks to find hardware-wall / saturation
  concurrency per input_length using TTFT, TPOT, ITL, and throughput.
  Use when the user asks about saturation point, hardware wall, 饱和点,
  性能墙, 推荐并发, capacity planning, or knee-point analysis on AutoRes data.
---

# 性能饱和点（Hardware Wall）分析

从 AutoRes `test_runs.metrics` 算出每个 `input_length` 下的**饱和并发**、推荐运行点与瓶颈归因。

**实现来源（单一）**：`autores/server/analysis/saturation.py`，已接入 AutoRes **chatbot**（`analyze_saturation` 工具）与 **MCP**（同名工具）。本目录的 `scripts/analyze_wall.py` 仅为薄 CLI 包装（只读 SQLite + argparse），计算逻辑全部 import 服务模块。

**何时优先用服务**：用户在 Web chatbot / 连着 AutoRes MCP 时，直接让 Agent 调 `analyze_saturation`，不必再跑本地脚本。本 skill 适用于：本地调试 CLI、服务未启动、或需要对照 METHODOLOGY 做深度解读。

**分工**：服务模块做可复现数值计算；本文件管流程与汇报；细节理论见 [METHODOLOGY.md](METHODOLOGY.md)。

## 何时使用

用户提到：饱和点 / hardware wall / 性能墙 / 推荐并发 / capacity planning / 膝点 / 多少并发到顶。

## 工作流

```
[1] 缩小 run 范围（MCP analyze_saturation / count_matching_runs，或 CLI --list）
[2] 跑 analyze_saturation（服务工具）或 analyze_wall.py（CLI）；禁止手算墙点
[3] 用输出表 + METHODOLOGY 决策表做归因
[4] 按下方模板汇报，写明置信度与陷阱
```

### 1. 选 run

固定对比前提：`gpu_type` + `model` + `framework` + 关键 params（`tp`/`dp` 等）+ `bench_flush_cache` + `prefix_rate`。

可用 MCP：`list_dimension_values`、`count_matching_runs`、`analyze_saturation`，或：

```bash
py .cursor/skills/perf-saturation-analysis/scripts/analyze_wall.py --list \
  --filter gpu_type=H20-141G --filter model=DeepSeek-V4-Flash
```

### 2. 跑分析

**推荐（服务已启动）**：经 chatbot 或 MCP 调用 `analyze_saturation`。

**本地 CLI**（在 AutoRes 仓库根目录，默认读 `config.yaml` → `database.path`）：

```bash
py .cursor/skills/perf-saturation-analysis/scripts/analyze_wall.py \
  --filter gpu_type=H20-141G --filter model=DeepSeek-V4-Flash \
  --slo-ttft-p99 2000 --slo-itl-p95 50 \
  --format md
```

| 参数 | 作用 |
|------|------|
| `--db PATH` | 覆盖默认库路径 |
| `--run-id ID` | 精确指定一个 run |
| `--filter k=v` | 可重复；等值过滤元信息列 |
| `--list` | 只列匹配 run，不分析 |
| `--plateau-gain` | 吞吐边际增益阈值，默认 `0.10` |
| `--latency-factor` | 延迟膝点倍数，默认 `2.0` |
| `--headroom` | 推荐运行点 = wall × headroom，默认 `0.8` |
| `--slo-ttft-p99` / `--slo-tpot-mean` / `--slo-itl-p95` / `--slo-e2e-p99` | SLO 上限（ms） |
| `--format md\|json` | 输出格式 |

**Agent 必须通过服务工具 `analyze_saturation` 或本 CLI 拿数值，不得凭肉眼扫矩阵估墙。**

### 3. 解读要点

脚本对每个 `input_length` 给出候选点（平台 / 峰值 / Kneedle / ITL 膝 / TTFT 膝 / USL `N*` / SLO），**wall = 最保守（最小）有效候选**。

瓶颈标签含义见 [METHODOLOGY.md](METHODOLOGY.md) 决策表。置信度：`high` / `medium` / `low`。

## 输出模板（向用户汇报）

```markdown
## 饱和点分析：{model} @ {gpu_type}

### 前提
- run_id / framework / params / flush / prefix_rate
- SLO（若有）

### 汇总
| input_length | wall并发 | 推荐运行点 | 峰值吞吐 | wall处吞吐 | wall处延迟 | USL N* | 瓶颈 | 置信度 |

### 逐 input_length 结论
- **{il}**：墙 ≈ c={wall}；推荐 ≤ {rec}；原因：{bottleneck}；陷阱：{caveat}

### 免责
（对应下方陷阱中实际触发的条目）
```

## 五条陷阱（汇报必须自检）

1. **客户端偏置**：sglang/vllm bench 单进程 asyncio + GIL，高并发可能虚高 TTFT。吞吐平稳而 TTFT 暴涨 → 先怀疑客户端。
2. **四指标不定根因**：算力 / KV / 调度需结合 `params` 与 GPU 监控。
3. **饱和非模型属性**：随输入分布变；random 与 fixed dataset 不可混比。
4. **缓存混淆**：`bench_flush_cache=false` 且 `prefix_rate>0` 会虚高吞吐，报告中必须标注。
5. **对比前提**：同 `gpu_type`/`model`/`params`；`PROMPTS=CONCURRENCY*5` 使高并发点噪声大 → 置信度下调。

## 判据摘要

| 检测器 | 规则 |
|--------|------|
| 吞吐平台 | 边际增益 `< plateau_gain`（默认 10%） |
| 峰值/回退 | `argmax(TP)`；后续明显低于峰值 → retrograde |
| 简化 Kneedle | 归一化曲线到首末连线最大偏差点 |
| 延迟膝点 | ITL（或 TPOT）相对最低并发基线 `> latency_factor` |
| TTFT 膝点 | 同上，用于区分 prefill 侧 |
| USL | ≥4 点拟合 α,β,γ；`N*=√((1-α)/β)`（β>0） |
| SLO | 全部 SLO 仍满足的最大并发 |

详情与公式：[METHODOLOGY.md](METHODOLOGY.md)。
