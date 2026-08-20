# 饱和点分析方法论

本文件供 Agent 在需要深入归因时阅读；日常流程以 [SKILL.md](SKILL.md) 为准。

## 1. 三区间模型（LLM serving）

固定 `input_length`，随 concurrency 上升，系统通常经历：

| 区间 | 特征 | 含义 |
|------|------|------|
| Compute-bound（低并发） | 吞吐近似线性 ↑，延迟接近单流基线 | GPU 未吃满，加并发几乎免费 |
| Saturation region（中并发） | 吞吐仍升但边际变缓 | 利用率提升区 |
| Memory / queue-bound（高并发） | 吞吐平台或回退，尾延迟陡升 | KV / HBM 带宽 / 排队主导 |

**Hardware wall（本 skill 的可操作定义）**：对该 `input_length`，再提高 concurrency **几乎不再提高吞吐**，且延迟相对基线明显恶化的临界并发。

饱和点是**运行时属性**（随序列长度分布、KV 精度、调度参数变化），不是模型常数。

参考：

- [Saturation point identification (The Neural Base)](https://theneuralbase.com/inference-optimization/learn/advanced/saturation-point-identification/)
- [Load testing methodology](https://theneuralbase.com/inference-optimization/learn/advanced/load-testing-methodology/)
- SLIM / attention decode 带宽饱和：[arXiv:2607.29575](https://arxiv.org/html/2607.29575v1)
- Vultr concurrency saturation sweeps：[docs](https://docs.vultr.com/inference-cookbook/rocm/benchmarks/saturation-analysis)

## 2. 指标角色

| 指标 | 回答什么 |
|------|----------|
| Output / Total Throughput | 还能不能更快（主判据） |
| TPOT | decode 算力 / batch 是否吃满 |
| TTFT | prefill 是否先顶 |
| ITL（尤其 P95/P99） | 用户侧排队是否主导（高并发比 TPOT 更敏感） |
| Request Throughput × E2E | Little's Law 交叉校验 |
| Completed | 失败/超时 → 硬墙 |

AutoRes metrics 键（入库后）：维度小写 `input_length` / `concurrency`；数值列保留 CSV 名如 `Output_Throughput`、`ITL_P99(ms)`。缺列（如 vllm 无 `Input_Throughput`、部分表无 P95）必须降级，不可崩溃。

`concurrency` 是压测的 `max_concurrency` 上限，**不是**实测在飞数。

## 3. Universal Scalability Law (USL)

Neil Gunther：

\[
X(N) = \frac{\gamma N}{1 + \alpha(N-1) + \beta N(N-1)}
\]

- \(\gamma\)：单负载吞吐量级  
- \(\alpha\)：争用（排队/串行）  
- \(\beta\)：一致性/协调开销（>0 时曲线有最大值后回退）

最优并发（\(\beta>0\)）：

\[
N^* = \sqrt{\frac{1-\alpha}{\beta}},\quad X_{\max}=X(N^*)
\]

### 线性化拟合（脚本实现）

令 \(y = N / X(N)\)：

\[
y = \frac{1}{\gamma}\left[1 + \alpha(N-1) + \beta N(N-1)\right]
\]

对基函数 \([1,\ (N-1),\ N(N-1)]\) 做 OLS，解出 \(1/\gamma,\ \alpha/\gamma,\ \beta/\gamma\)，再反解参数。

要求 ≥4 个有效并发点；\(\beta\le 0\) 时不报 \(N^*\)（无 retrograde）。

参考：[USL manifesto (PerfDynamics)](https://www.perfdynamics.com/Manifesto/USLscalability.pdf)、[usl R vignette](https://cran.r-project.org/web/packages/usl/vignettes/usl.pdf)

## 4. 简化 Kneedle

原算法（Satopaa et al., 2011）对曲线平滑后找相对首末连线的最大曲率近似点。  
压测矩阵点少且近似单调，脚本用**无样条**版本：

1. 将 \((N, X)\) 归一化到单位方形  
2. 取到首末点连线**垂直距离**最大的点作为膝点  

灵敏度不如完整 Kneedle；与平台点、延迟膝点交叉验证。

参考：[`kneed`](https://kneed.readthedocs.io/en/stable/) / [paper PDF](http://www.ecs.umass.edu/irwin/simplex.pdf)

## 5. Little's Law 交叉校验

\[
L = \lambda W
\]

用 `Request_Throughput`（req/s）与 `E2E_Mean(ms)/1000` 估实测在飞数 \(L\)。  
若 \(L\) 远大于设定 `concurrency`，或随并发线性放大而吞吐不动 → 排队主导。

生产经验：常在 ~70% 利用率以下留 headroom，避免尾延迟非线性爆炸。

参考：Anyscale [LLM metrics / goodput](https://docs.anyscale.com/llm/serving/benchmarking/metrics.md)；队列与 LLM：[tianpan LLM queuing](https://tianpan.co/blog/2026-04-10-llm-queuing-theory-littles-law-token-scheduling)

**Goodput**：满足 SLO 的成功请求比例。本 skill 的 SLO 约束最大并发即 goodput≈100% 的上界代理。

## 6. 检测器组合 → wall

| 检测器 | 规则（默认） |
|--------|----------------|
| 吞吐平台 | 相对上一档增益 `< 10%` |
| 峰值 | `argmax(Output_Throughput)`；后续低于峰值×(1−tol) → retrograde |
| 简化 Kneedle | 见 §4 |
| ITL 膝 | ITL_P95（回退 Mean / TPOT）> `2×` 最低并发基线 |
| TTFT 膝 | 同上，区分 prefill |
| USL \(N^*\) | §3 |
| SLO 上限 | 全部给定 SLO 仍满足的最大并发 |

**wall** = 上述有效候选的**最小值**（最保守）。  
**推荐运行点** = `round(wall × headroom)`，默认 headroom=0.8。

## 7. 瓶颈决策表

| 现象 | 判定标签 | 建议动作 |
|------|----------|----------|
| 吞吐平台 + 延迟陡升 | `queue_buildup` | 调 `max_running_requests`、加 dp |
| 吞吐下降 + 延迟相对平稳 | `kv_or_memory` | 降 batch / 查 mem_fraction、量化 KV |
| TTFT 先爆、TPOT 稳、吞吐仍涨 | `prefill_bound` | 调 `chunked_prefill_size`、考虑 PD |
| TPOT/ITL 先爆、TTFT 稳 | `decode_bound` | 加 tp、投机解码、dcp |
| Completed 下降或大量缺失 | `hard_overload` | 已过硬墙，降并发 |
| β>0 且 \(N^*\) 远小于实测高并发 | `coherence_overhead` | 查 EP、moe_a2a、跨机通信 |
| 吞吐平台但 TTFT 暴涨、极高并发 | `client_bias_suspect` | 先排除 bench 客户端 GIL |
| 证据不足 / 点数过少 | `inconclusive` | 加密集 concurrency 扫点 |

## 8. 置信度

| 等级 | 条件 |
|------|------|
| `high` | ≥5 并发点；平台与延迟膝相差 ≤1 档；无明显客户端偏置迹象 |
| `medium` | 3–4 点，或候选点分散 1–2 档 |
| `low` | <3 点；仅外推 USL；或 flush/prefix 热缓存未标注清楚 |

## 9. 测量偏置（必读）

单进程 asyncio bench 可引入客户端排队，使 TTFT/TPOT 随负载虚高（GIL / M/G/1 客户端模型）。见 [arXiv:2605.24217](https://arxiv.org/html/2605.24217v2)。

解读规则：若 Output_Throughput 已平台而仅 TTFT 暴涨 → 标注 `client_bias_suspect`，勿武断写「GPU 算力顶满」。
