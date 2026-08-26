# 性能饱和点分析 — 外部方法论参考（离线存档）

本目录存放 AutoRes `perf-saturation-analysis` skill 引用的外部方法论原文快照，便于内网离线阅读。  
**下载日期**：2026-08-24

> 内容为抓取时的网页/PDF 快照，非官方镜像。链接失效或页面更新时，请用下方「重新下载」命令刷新。

## 文件索引

| 本地文件 | 来源 | 用途 |
|----------|------|------|
| [neural-base-saturation-point.html](./neural-base-saturation-point.html) | [The Neural Base — Saturation point identification](https://theneuralbase.com/inference-optimization/learn/advanced/saturation-point-identification/) | 三区间模型：compute → saturation → memory/queue |
| [neural-base-load-testing.html](./neural-base-load-testing.html) | [The Neural Base — Load testing methodology](https://theneuralbase.com/inference-optimization/learn/advanced/load-testing-methodology/) | LLM 压测方法论（METHODOLOGY.md 同系列参考） |
| [usl-scalability.pdf](./usl-scalability.pdf) | [PerfDynamics — USL Scalability Manifesto (Neil Gunther)](https://www.perfdynamics.com/Manifesto/USLscalability.pdf) | Universal Scalability Law；AutoRes `saturation.py` USL 拟合的理论基础 |
| [anyscale-llm-serving-metrics.html](./anyscale-llm-serving-metrics.html) | [Anyscale Docs — LLM latency & throughput metrics](https://docs.anyscale.com/llm/serving/benchmarking/metrics) | TTFT / ITL / throughput / goodput、Little's Law 导向 |
| [arxiv-slim-2607.29575.html](./arxiv-slim-2607.29575.html) | [arXiv:2607.29575 — SLIM](https://arxiv.org/html/2607.29575v1) | Attention decode 带宽饱和分析 |
| [vultr-saturation-analysis.html](./vultr-saturation-analysis.html) | [Vultr Inference Cookbook — Saturation analysis](https://docs.vultr.com/inference-cookbook/rocm/benchmarks/saturation-analysis) | Concurrency sweep 实践 |

## 与 AutoRes 的对应关系

| 外部方法论 | AutoRes 实现 |
|------------|--------------|
| Neural Base 三区间 + wall | `autores/server/analysis/saturation.py` — 平台点 / Kneedle / 延迟膝 |
| USL \(N^*\) | `saturation.py` — `fit_usl()` |
| Anyscale 指标角色 + SLO | `analyze_saturation` — `--slo-*` 参数；METHODOLOGY.md §2 |
| SLIM decode 带宽 | 瓶颈标签 `decode_bound` 解读参考 |
| Vultr sweep | `vllm_sgl_benchs.sh` / `vlm_benchs.sh` concurrency 矩阵设计参考 |

## 阅读方式

- **PDF**：直接用 PDF 阅读器打开 `usl-scalability.pdf`。
- **HTML**：浏览器打开对应 `.html` 文件即可（部分页面依赖 CDN 样式/脚本，离线时排版可能略差，正文通常在 HTML 内）。

## 重新下载

在 AutoRes 仓库根目录执行（需可访问外网）：

```powershell
$dir = "docs/methodology-references"
$urls = @(
  @{ name = "neural-base-saturation-point.html"; url = "https://theneuralbase.com/inference-optimization/learn/advanced/saturation-point-identification/" },
  @{ name = "neural-base-load-testing.html"; url = "https://theneuralbase.com/inference-optimization/learn/advanced/load-testing-methodology/" },
  @{ name = "usl-scalability.pdf"; url = "https://www.perfdynamics.com/Manifesto/USLscalability.pdf" },
  @{ name = "anyscale-llm-serving-metrics.html"; url = "https://docs.anyscale.com/llm/serving/benchmarking/metrics" },
  @{ name = "arxiv-slim-2607.29575.html"; url = "https://arxiv.org/html/2607.29575v1" },
  @{ name = "vultr-saturation-analysis.html"; url = "https://docs.vultr.com/inference-cookbook/rocm/benchmarks/saturation-analysis" }
)
foreach ($item in $urls) {
  curl.exe -fsSL -A "AutoRes/1.0 (methodology archive)" -o (Join-Path $dir $item.name) $item.url
  Write-Host "OK $($item.name)"
}
```
