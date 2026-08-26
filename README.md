# AutoRes — 性能测试结果管理与报告 Agent

自动采集 **sglang / vllm / vllm-ascend** 的性能测试结果入库（SQLite），并通过 Web chatbot 用自然语言按需生成 Excel 对比报告。

完整设计见 [docs/design.md](docs/design.md)。

## 能做什么

| 能力 | 说明 |
|------|------|
| 自动入库 | Scanner 定时扫描 NAS 时间戳目录，解析 `result.csv` + `metadata.json` |
| 手工上传 | 数据不在 NAS 时，在 `/upload` 提交 CSV + 启动命令 + 模型 `config.json`（可选）+ 元信息；支持 **单机/分布式** 与 **PD 分离** 两种部署模式 |
| 参数推导 | 启动命令 + 模型 `config.json` 一起推出**实际生效**的启动参数（`context_length` / `dtype` / `quantization` / 批量调度默认值…），算法照搬 vllm / sglang 上游，并逐项记录来源 |
| 自然语言对比 | Chatbot 多轮澄清需求 → 确定性流水线查库对齐 → 下载 Excel |
| 跨框架参数对齐 | `tools/param_map.py` 维护 vLLM ↔ SGLang 启动参数配对（含量纲/类型差异说明） |
| PD 分离部署 | `tools/param_map_pd.py` 解析 prefill/decode/router 参数；入库 `prefill_*` / `decode_*` 前缀列 |
| 卡数自动计算 | 入库时按 tp×pp×dp（sglang 开 dp_attention 时不乘 dp）计算 `gpu_count`；PPU 系列按 16 卡/机计 |
| KV cache 命中率 | 统一列 `KV_Cache_Hit_Rate(%)`，**跨框架可比** |
| Spec decoding 指标 | 框架专属列（sglang accept length / vllm accept rate+length），**跨框架不可比** |
| Excel 对比报告 | 双层表头矩阵宽表；支持多取值两两差异列与按 `Input_Length` 的块汇总；可选卡数弱扩展归一 |

## 组成

```
压测                         落盘                         服务
────                         ────                         ────
tools/vllm_sgl_benchs.sh ──► bench JSON 目录（text）
tools/vlm_benchs.sh ───────► bench JSON 目录（vlm）
                                    │
tools/to_csv.py ──────────► NAS 时间戳目录 ──► Scanner ──┐
frontend/upload.html / upload_vlm.html ──────► API 上传 ──┤──► SQLite
                                                           │   test_runs / vlm_test_runs
浏览器 chatbot / 下载 ◄── API + Agent + 报告流水线 ◄───────┘
```

- **压测脚本**
  - `tools/vllm_sgl_benchs.sh`：纯文本，循环 concurrency → input_len → **prefix_rate**，抓取 KV / spec 指标。
  - `tools/vlm_benchs.sh`：多模态，循环 concurrency → input_len → **image_count** → **image_resolution**（`VIDEO_COUNT` 保留且必须为 0）。
- **维度注入** `tools/inject_dims.py`：把本轮行键写入结果 JSON 的 `_autores_dims`，供 `to_csv` 优先读取。
- **落盘脚本** `tools/to_csv.py`：`--benchmark-kind text|vlm`，整理为固定 schema 的 `result.csv` + `metadata.json`。
- **Scanner**（`autores/scanner/`）：读 `metadata.benchmark_kind`，路由到 `test_runs` 或 `vlm_test_runs`。
- **API + 前端**（`autores/server/` + `frontend/`）：
  - `/` — chatbot（SSE）
  - `/upload` — 文本压测手工上传
  - `/upload/vlm` — VLM 压测手工上传
  - `/api/chat`、`/api/upload`、`/api/download/{token}`、`/api/health`
- **参数工具**（`tools/`）：
  - `param_map.py` — vLLM/SGLang 启动参数配对表
  - `param_map_pd.py` — PD 分离 / kv-transfer / router 参数解析
  - `gpu_count.py` — 入库时卡数计算与默认值回填
  - `verify_param_map.py` — 对照上游源码校验 flag 是否仍存在
  - `gpu_memory_presets.py` — 显存档位与 PPU 每机卡数规则

---

## 测试人员工作流（推荐）

典型流程：**先压测 → 再落盘**。两个脚本通过 **`--framework` / `FRAMEWORK`** 区分框架，落盘时 `--framework` 必须与压测时一致。

```
┌─────────────────────────────────────────────────────────────────┐
│ 0. 启动推理服务（见下文「Server 前置条件」）                      │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ 1. tools/vllm_sgl_benchs.sh                                     │
│    输出：./logs_.../*.json（每个 并发×输入长度 一个文件）         │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. tools/to_csv.py                                              │
│    输入：上一步 JSON 目录 + 元信息 + 启动命令                    │
│    输出：{NAS}/YYYYMMDD_HHMMSS/result.csv + metadata.json         │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
                    Scanner 自动入库（或 /upload 手工上传）
```

### 0. Server 前置条件（与指标抓取相关）

| 指标 | sglang server | vllm server |
|------|---------------|-------------|
| **KV cache hit rate** | 启动加 `--enable-cache-report`；bench 使用 `--cache-report`（脚本已加） | 开启 prefix caching；脚本通过 `/metrics` 的 `prefix_cache_queries/hits` 算 delta |
| **Spec decoding** | 启动投机解码（如 `--speculative-algorithm` 等）；bench 自动读 `/server_info` 的 `avg_spec_accept_length` | 开启 speculative decoding；bench 自动读 `/metrics` 的 `spec_decode_*` |
| **清缓存（可选）** | `POST /flush_cache` | `POST /reset_prefix_cache`（需 `VLLM_SERVER_DEV_MODE=1`） |

抓不到的指标**不会阻断压测**，对应 CSV 列为 `N/A`。

> **关于 `FLUSH_CACHE=1`**：每轮压测前清缓存会把 KV hit rate 压到冷启动水平。想测 workload 下的**自然命中率**时保持默认 `0`。

### 1. 压测：`tools/vllm_sgl_benchs.sh`

在 **Linux / bash** 环境运行（依赖 `python3`、`curl`；vllm 分支还需已安装 `vllm` CLI）。

**最简用法（sglang，默认）：**

```bash
cd AutoRes/tools

# 按需改 server / 模型（或直接编辑脚本顶部默认值）
export FRAMEWORK=sglang
export SERVER_HOST=30.205.160.45
export SERVER_PORT=18000
export MODEL=deepseek_v4
export TOKENIZER=/path/to/tokenizer

bash vllm_sgl_benchs.sh
```

**vllm 示例：**

```bash
export FRAMEWORK=vllm
export SERVER_HOST=127.0.0.1
export SERVER_PORT=8000
export MODEL=/path/to/model
export TOKENIZER=/path/to/tokenizer

# vllm server 需：投机解码（若要 spec 指标）、prefix caching（若要 KV 指标）
# 可选：export VLLM_SERVER_DEV_MODE=1  （仅当 FLUSH_CACHE=1 时需要）

bash vllm_sgl_benchs.sh
```

**常用环境变量：**

| 变量 | 默认 | 说明 |
|------|------|------|
| `FRAMEWORK` | `sglang` | `sglang` 或 `vllm`（决定 bench 命令与指标抓取方式） |
| `SERVER_HOST` / `SERVER_PORT` | 见脚本 | 推理服务地址 |
| `MODEL` / `TOKENIZER` | 见脚本 | 模型名与 tokenizer 路径。`TOKENIZER` 就是模型目录，落盘时 `MODEL_CONFIG` 默认取它下面的 `config.json` |
| `FLUSH_CACHE` | `0` | `1` = 每轮压测前清 server KV/prefix cache |
| `WARMUP_REQUESTS` | `0` | 每轮正式压测前的预热请求数。`0` = 不预热；`>0` 由脚本自己发，两个 bench 的原生 warmup 一律关掉，保证 sglang / vllm 行为一致 |
| `WARMUP_OUTPUT_LEN` | `32` | 预热请求的输出长度（对齐 sglang 原生 warmup 的 32 token 上限） |
| `PREFIX_RATES` | `(0.0)` | 共享前缀比例**数组**（每项 `0~1`），作为第 3 层循环。每轮真实前缀长度 = `round(INPUT_LEN × rate)`，正文 = `INPUT_LEN − 前缀`。写入 metrics 行键 `Prefix_Rate`（不再是表列） |

> **关于 `PREFIX_RATES`**（共享前缀，测前缀缓存命中）：
> - `vllm` bench：用 `random` 数据集的 `--random-prefix-len`（单一全局前缀，全体请求共享）。
> - `sglang` bench：`random` 数据集**无前缀参数**，脚本在 rate>0 时自动改用
>   `generated-shared-prefix` 数据集（`--gsp-num-groups 1` 单一全局前缀 + `--gsp-system-prompt-len=前缀` +
>   `--gsp-question-len=正文`）逆向映射逼近 vllm，落盘 `Input_Length` 仍为总输入。
> - rate=`0` → 无前缀（vllm `--random-prefix-len 0`；sglang 沿用 `random-ids`）。
> - 输出文件名带 `_pr{rate}`，不同 rate 互不覆盖；每轮结束后 `inject_dims.py` 写入 `_autores_dims`。

> **关于 `WARMUP_REQUESTS`**（预热，两框架强制对齐）：
> 两个 bench 的原生 warmup 差异很大 —— sglang `--warmup-requests` **默认就是 1**，输出截到 32 token，一次性全发；
> vllm `--num-warmups` 默认 0，输出用完整 `OUTPUT_LEN`，且受 `--max-concurrency` 限流；「预热后清缓存」也只有 sglang 有
> （`--flush-cache`）。另外 server=vllm 时 KV hit rate 靠 `/metrics` 计数器 delta，原生 warmup 会混进 delta，
> 而 sglang `--cache-report` 只统计正式请求，两者口径天然不一致。
> 所以脚本把两边的原生 warmup 都显式传 `0`，预热改由脚本统一发（同一条 `/v1/chat/completions`，条数 / 输出长度 / 并发一致），
> 并把预热放在 **KV 基线采样之前**；`FLUSH_CACHE=1` 时预热后再清一次缓存，保证正式压测仍是冷启动。
> 这样 4 种 server×bench 组合的预热强度、清缓存时机、KV hit rate 口径完全一致。
> 若 bench 版本太老不认这两个参数，脚本会在启动横幅告警（sglang 会多出原生的 1 条预热）。

**输出：** 脚本内 `LOG_SUBDIR` 目录（默认 `tools/logs_910b_cjb_dsv4flashint8_8_260723/`）下多个 JSON。已存在的文件会跳过，可断点续跑。

**脚本内可改项：** `OUTPUT_LEN`、并发列表 `max_concurrency`、`input_length_map`（高并发时缩短输入长度）、`LOG_SUBDIR`、输出文件名前缀等。

**各框架 bench 命令摘要：**

| | sglang | vllm |
|---|--------|------|
| 命令 | `python3 -m sglang.bench_serving --backend sglang-oai-chat ...` | `vllm bench serve --backend openai ...` |
| KV hit rate | `--cache-report` → JSON 内 `cache_report.cache_hit_rate_pct` | 脚本前后抓 `/metrics`，注入 `kv_cache_hit_rate` |
| Spec | JSON 内 `accept_length` | JSON 内 `spec_decode_acceptance_rate` / `spec_decode_acceptance_length` |

### 1b. VLM 压测：`tools/vlm_benchs.sh`

多模态压测，循环 **concurrency → input_len → image_count → image_resolution**，入库表 `vlm_test_runs`。

```bash
cd AutoRes/tools
# 编辑脚本顶部：MODEL / TOKENIZER / IMAGE_COUNTS / IMAGE_RESOLUTIONS 等
bash vlm_benchs.sh
```

**关键配置：**

| 变量 | 默认 | 说明 |
|------|------|------|
| `IMAGE_COUNTS` | `(1)` | 每请求图片数数组（第 3 层循环） |
| `IMAGE_RESOLUTIONS` | `("720x1280")` | `HxW` 字符串数组（第 4 层循环） |
| `VIDEO_COUNT` | `0` | 保留标量；**>0 直接报错退出**（sglang image 数据集无视频合成） |
| `WARMUP_REQUESTS` | `0` | 带图预热（chat message 挂 N 张 HxW data-URL）；有 PIL 用随机图，无则退化小图并告警 |

**两框架参数映射（脚本内完成）：**

- sglang：`--dataset-name image --image-count N --image-resolution HxW --image-format jpeg --image-content random`
- vllm：`--dataset-name random-mm --random-mm-base-items-per-request N --random-mm-num-mm-items-range-ratio 0 --random-mm-limit-mm-per-prompt '{"image":N,"video":0}' --random-mm-bucket-config '{"(H, W, 1)":1.0}'`（backend=`openai-chat`）

启动时对上述 flag 做 `--help` 探测，**缺失则硬失败**（不静默降级）。落盘自动带 `--benchmark-kind vlm`。

#### 不可对齐 / 待验证清单

- sglang `--image-format` / `--image-content` 在 vllm **无等价开关** → 不可跨框架对齐这两项。
- vllm 支持每请求图片数抖动（`--random-mm-num-mm-items-range-ratio`），sglang 无 → 脚本固定为 `0`。
- 视频：sglang `image` 数据集无视频合成 → `VIDEO_COUNT` 仅保留字段，`>0` 报错。
- `Total_Input_Tokens` 在 VLM 下是否含图像 token，两框架口径**待实测**；落地前标注为**不可跨框架比较**。
- vllm `--random-mm-bucket-config` 与 sglang image 数据集的具体 flag 名需按目标版本核对（脚本启动探测硬失败）。

### 2. 落盘：`tools/to_csv.py`

把上一步 JSON 目录转为 Scanner 可识别的 NAS 目录。

**sglang 示例：**

```bash
python tools/to_csv.py \
  --benchmark-kind text \
  --framework sglang --bench-framework sglang \
  --bench-flush-cache false \
  --framework-version 0.4.6 \
  --input-dir ./tools/logs_910b_cjb_dsv4flashint8_8_260723 \
  --nas-dir /mnt/nas/benchmark_root \
  --gpu-type H20-141G \
  --model DeepSeek-V4 \
  --model-config /models/DeepSeek-V4/config.json \
  --launch-cmd "python -m sglang.launch_server --tp-size 8 --enable-cache-report --speculative-algorithm EAGLE"
```

> `--framework`（server 框架）与 `--bench-framework`（压测工具框架）**相互独立、均必填**，禁止默认一致——
> sglang bench 可打 vllm server，反之亦然，共 4 种组合。`--bench-flush-cache true/false` 记录压测前是否清缓存，
> 作为结果对比的区分维度入库（**必填**）。`--benchmark-kind` 决定路由到 `test_runs` 还是 `vlm_test_runs`。
> 行键（如 `Prefix_Rate` / `Image_Count`）来自 JSON 的 `_autores_dims` 或 CSV 列，**不再**通过 CLI 整份回退。

**vllm 示例：**

```bash
python tools/to_csv.py \
  --benchmark-kind text \
  --framework vllm --bench-framework vllm \
  --bench-flush-cache false \
  --framework-version 0.5.12 \
  --input-dir ./tools/logs_vllm \
  --nas-dir /mnt/nas/benchmark_root \
  --gpu-type H800 \
  --model Qwen2.5-72B \
  --launch-cmd "vllm serve Qwen2.5-72B -tp 8 --enable-prefix-caching" \
  --bench-cmd "vllm bench serve --random-input-len 1024 --percentile-metrics ttft,tpot,itl,e2el"
```

> 压测脚本 `vllm_sgl_benchs.sh` / `vlm_benchs.sh` 顶部把 `RUN_TO_CSV=1` 时，**压测跑完会自动调用一次 `to_csv.py`**
> （带对应 `--benchmark-kind`；`--bench-flush-cache` 由脚本的 `FLUSH_CACHE` 派生），无需手动执行本步。
> 模型 `config.json` 也不用另配：`MODEL_CONFIG` 留空即取 `$TOKENIZER/config.json`，
> 于是 `MODEL_PARAMS_B` / `MODEL_WEIGHT_GB` / `MODEL_DTYPE` 三项都可以留空、全部按 config 推导。

**vllm-ascend：** 与 vllm 相同，仅 `--framework vllm-ascend`；参数解析走 vllm 分支，入库 `framework` 仍存 `vllm-ascend`。

**要点：**

- `--benchmark-kind`：`text`（默认）或 `vlm`，写入 `metadata.benchmark_kind` 并决定 Scanner 入库表。
- `--input-dir`：压测 JSON 所在目录（sglang / vllm 均为 `*.json` 整文件解析）。
- `--framework` / `--bench-framework`：分别是 server 框架与压测工具框架，**均必填且相互独立**。
  `--bench-framework` 决定 bench JSON 字段解析（须与压测所用工具一致），`--framework` 决定 `--launch-cmd` 的参数提取。
- `--bench-flush-cache`：`true/false`，**必填**，记录压测前是否清缓存（flush=冷启动、不 flush=复用缓存，结果差异大，作为入库对比维度）。
- 行键维度（`Prefix_Rate` / `Image_*`）由 `inject_dims.py` 写入 `_autores_dims`，或直接出现在 CSV；缺列记 N/A，**不提供** CLI / 表单整份回退值。
- vllm / vllm-ascend 建议仍传 `--bench-cmd` 作兜底；优先读 `_autores_dims.random_input_len`。
- vllm bench 须含 `--percentile-metrics ttft,tpot,itl,e2el` 才有完整 E2E/ITL 列（压测脚本已包含）。
- `--launch-cmd`：完整服务启动命令；脚本提取 tp/dp/pp、投机解码、prefix caching 等入库维度，并计算 `gpu_count`。
- `--model-config`：模型目录下的 `config.json`，**强烈建议传**。`context_length`、`dtype`、`quantization`、
  `max-num-batched-tokens` / `chunked-prefill-size` 这些参数**启动命令里通常不写**，是 vllm / sglang 读模型 config
  在运行时推导的；不传则相应列留空。推导算法逐项照搬上游源码，见 `tools/model_config.py`。
  三个模型元信息列也靠它推导（见下）。
  用压测脚本自动落盘时不必单独配：`MODEL_CONFIG` 留空即取 `$TOKENIZER/config.json`
  （随机数据集压测必须给 bench 传 `--tokenizer`，那个路径就是模型目录）。
- `--model-params-b` / `--model-weight-gb` / `--model-dtype`：参数量（单位 B）、权重实际占用（单位 GiB）、
  权重精度（`bf16|fp16|fp8|int8|int4|fp4`），对应同名表列。**传了 `--model-config` 就三项都不用给**，
  全部按 config 推导；给了命令行值则以命令行为准、与推导值不符时告警。
  没给 `--model-config`（或 config 缺 `num_hidden_layers` / `hidden_size` / `vocab_size`）时
  `--model-params-b` 变必填——它是分组对比的主轴，不能为空。
- 成功后在 `--nas-dir` 下创建 `YYYYMMDD_HHMMSS/`，含 `result.csv`、`metadata.json`
  （传了 `--model-config` 还有一份 `model_config.json` 原文，供日后按新版规则重算）。

**参数来源留痕**：入库的 `params` 是**实际生效值**（命令写了用写的，没写按上游逻辑推导）。
为了不丢"是不是用户显式设的"，`extra` 里同时留 `params_explicit`（只含命令写了的）、
`param_sources`（每项来源 `explicit`/`config`/`gpu`/`static`）、`param_notes`（推导说明与告警）、
`model_arch`（层数 / KV 头数 / head_dim / 单 token KV 字节数 / MoE / MLA / vision 等）、
`model_meta`（元信息三列的推导值原始留档，便于和用户手填值对账）。
sglang 的 `mem_fraction_static`、`max_running_requests`、`attention_backend` 依赖运行时状态算不准，
**不推导、列留空**——编个数字比不给更误导。

**模型元信息三列的口径**（曾经的 `model_size` 口径不清，已拆开）：

| 列 | 单位 | 来源 | 说明 |
|----|------|------|------|
| `model_params_b` | B（10⁹） | config 推导，可手工覆盖 | 7B 模型记 `7.62`。按形状字段逐块累加：稠密 / MoE / 两代 Qwen-VL 实测与官方参数量**完全吻合**，仅 DeepSeek-V3 差 0.3%（MTP 层近似）。核心形状字段缺任一项则留空，不给近似数 |
| `model_weight_gb` | GiB | config 推导，可手工覆盖 | 与 `gpu_memory_presets.GPU_MEMORY_GIB` 同单位，可直接和显存比。量化 checkpoint 按「层内线性层走量化精度，embedding / lm_head / vision tower 走 `torch_dtype`」分段算 |
| `model_dtype` | — | config 推导，可手工覆盖 | **权重**精度，不是框架的计算 dtype。量化 checkpoint 的 `torch_dtype` 多是 `bfloat16`（激活精度），真实权重精度读 `quantization_config`；DeepSeek-V3 因此是 `fp8` 而非 `bf16` |

`quant_method` → 权重精度的映射表取 vllm `QUANTIZATION_METHODS` 与 sglang
`BASE_QUANTIZATION_METHODS` 的并集；`torchao` / `gguf` / `MIXED_PRECISION` 等逐层位宽不同的，
**不推导、列留空**并在 `param_notes` 里写明原因。

多模态的参数量含 vision tower、patch embedding 与 Qwen-VL 系的 patch merger。注意两代
Qwen-VL 的 `vision_config` 键名是反的（2.5 代 `hidden_size` 是内部宽度、`out_hidden_size` 是
输出宽度；2 代 `embed_dim` 才是内部宽度、`hidden_size` 是输出宽度），且 2.5 代的 vision MLP
是 gated 的、2 代不是——两处都按"哪个键存在 / 激活函数是什么"判断，见 `tools/model_config.py`。
CLIP / SigLIP 那类 projector 形状不在 `vision_config` 里，会少算几十 M 并在 `param_notes` 里写明。

---

## 指标列说明（result.csv）

除吞吐/延迟外，新增列如下：

| CSV 列 | 跨框架可比 | sglang 来源 | vllm 来源 |
|--------|------------|-------------|-----------|
| `KV_Cache_Hit_Rate(%)` | **是** | `cache_report.cache_hit_rate_pct` | 脚本注入的 `kv_cache_hit_rate` |
| `SGLang_Spec_Accept_Length` | 否（仅 sglang） | `accept_length` | `N/A` |
| `vLLM_Spec_Accept_Rate(%)` | 否（仅 vllm） | `N/A` | `spec_decode_acceptance_rate` |
| `vLLM_Spec_Accept_Length` | 否（仅 vllm） | `N/A` | `spec_decode_acceptance_length` |

对比报告里 **`KV_Cache_Hit_Rate(%)` 可直接跨框架比较**；spec 三列各框架各填各的，另一框架为 `N/A`，不要跨框架对比 accept rate 与 accept length 的数值含义。

---

## 快速开始（服务与上传）

### 手工上传（可选）

服务启动后打开 `http://<服务器>:8080/upload`（文本）或 `/upload/vlm`（多模态）：

1. 上传符合固定 schema 的结果 CSV——**必需列**只有 `Input_Length`、`Concurrency`；
   可选行键列（text：`Prefix_Rate`；vlm：`Image_Count` / `Video_Count` / `Image_Resolution`）缺则整列 N/A，且不与有值的行对齐。
   **选好文件后会自动按 spec 列识别 bench 框架**
2. **上传模型 `config.json`（可选，强烈建议）**——`context_length` / `dtype` / `quantization` /
   `max-num-batched-tokens` 这些参数启动命令里通常不写，靠它才能推出来。
   选好文件后会立刻回显识别到的架构、层数、KV 头数、量化方式，以及推导出的参数量 / 权重占用 /
   权重精度（写进对应输入框的 placeholder，不预填），当场发现传错文件。
3. 粘贴启动命令（支持 `#` 注释、空行、反斜杠续行）
4. 选择 **单机/分布式** 或 **PD 分离**（检测到 disaggregation / kv-transfer 参数会自动切换）
5. 填写模型服务信息：`framework`（server 框架：`sglang` / `vllm` / `vllm-ascend`）、`framework_version`、
   `model`，选择 `gpu_type`。
   **参数量（B）**、**权重占用（GiB）**、**权重精度** 三项留空即按 `config.json` 推导，
   核对上一步回显的推导值就行，不必手填；填了以填的为准，不一致时回显告警。
   未上传 `config.json` 时**参数量必填**。
6. **bench 参数（必填）**：
   - **bench 框架**：与 server 框架相互独立。上传 CSV 后按 spec decoding 列是否有值自动预填
     （仅 vLLM 列有值→`vllm`，仅 SGLang 列有值→`sglang`；两者都有/都无则需手选），可手动改。
   - **是否 flush cache**：无法从 CSV 推断，**必须手动勾选**（flush=冷启动、不 flush=复用缓存）。
   - 表单**不再**提供 `prefix_rate` 整份回退值（已下沉为行键）。

启动参数提取与 `to_csv.py` 同一套规则；PD 模式下分别填写 prefill / decode / router 启动命令
（两个角色跑同一个模型，共用同一份 `config.json`）。提交成功后的回显会给每个参数标出来源：
无标记 = 命令显式写的，`←config` = 从模型 config 推的，`←显存` = 按显卡显存档位推的，`←默认` = 上游静态默认值。

#### 样例：`result.csv`（节选表头 + 一行）

完整表头以 `tools/to_csv.py` 的 `METRIC_FIELD_MAP` 为准；**必填列**只有 `Input_Length`、`Concurrency`，缺测填 `N/A`。

```csv
Input_Length,Concurrency,...,Completed,Failed,Total_Input_Tokens,Total_Output_Tokens,KV_Cache_Hit_Rate(%),SGLang_Spec_Accept_Length,vLLM_Spec_Accept_Rate(%),vLLM_Spec_Accept_Length
1024,32,...,400,0,409600,81920,63.5,2.87,N/A,N/A
```

另含延迟 Std/P90 列（如 `TTFT_Std(ms)`、`TTFT_P90(ms)` 等），完整表头见 `METRIC_FIELD_MAP`。  
sglang 行：`KV_Cache_Hit_Rate` + `SGLang_Spec_Accept_Length` 有值，vllm spec 列为 `N/A`。  
vllm 行反之；`KV_Cache_Hit_Rate(%)` 两边语义对齐（均为 0–100 的百分比）；`Input_Throughput` 由 total−output 派生。

#### 样例：`launch.txt`（sglang）

```text
python -m sglang.launch_server \
  --model-path /models/GLM-4.5 \
  --tp-size 8 \
  --mem-fraction-static 0.85 \
  --enable-hierarchical-cache \
  --enable-cache-report \
  --attention-backend flashinfer
```

#### 样例：`launch.txt`（vllm）

```text
vllm serve /models/Qwen2.5-72B \
  -tp 8 \
  --enable-expert-parallel \
  --gpu-memory-utilization 0.9 \
  --enable-prefix-caching
```

### 部署服务

**方式 A：裸机运行**（Debian 12 等）

```bash
bash scripts/install.sh
bash scripts/start.sh     # 首次会从 config.example.yaml 生成 config.yaml
bash scripts/status.sh
bash scripts/stop.sh
```

编辑 `config.yaml` 中的 `llm.base_url` 与 `scanner.benchmark_root`（= 测试人员 `--nas-dir`）。日志在 `var/log/`，数据库在 `var/data/autores.db`。

**方式 B：Docker Compose**

```bash
cp config.example.yaml config.yaml
docker compose up -d
```

前端：

- Chatbot：`http://<服务器>:8080/`
- 手工上传：`http://<服务器>:8080/upload`

---

## Excel 报告版式

报告为纯数据对比表（无图表、无 LLM 结论）：

- **行**：`(Input_Length, Concurrency)` 测试条件，按输入长度分块
- **列**：每个指标一组；组内按对比轴取值展开（含 `KV_Cache_Hit_Rate(%)` 等动态指标列）
- **双层表头**：第 1 行指标名，第 2 行对比轴取值
- **差异列**：对比轴两个及以上取值时，两两 `A vs B` 相对差异（百分比）
- **块汇总**：每个 `Input_Length` 块末尾一行差异列均值
- **卡数弱扩展**（可选）：吞吐类 × 卡数比例、concurrency 同比对齐；延迟类保持原值

---

## 结构化启动参数

入库维度与 `tools/param_map.py` 对齐，包括并行度（tp/pp/dp/dcp、ep_enabled/ep_width）、显存与 KV、调度、量化、投机解码、hicache 等。部分参数跨框架**量纲或类型不同**，配对表中有说明。

PD 分离额外入库：`deployment_mode=pd_disagg`、`prefill_*` / `decode_*` 镜像列、`router_*`、`pd_transfer_backend`、`prefill_gpu_count` / `decode_gpu_count`。

维护参数表后请运行：

```bash
python tools/verify_param_map.py
```

---

## 依赖

```bash
pip install -r requirements.txt          # 生产（Python ≥ 3.11）
pip install -r requirements-dev.txt      # 含测试依赖
```

主要运行时依赖：FastAPI、uvicorn、openpyxl、openai、PyYAML、python-multipart。数据库为标准库 `sqlite3`。

压测脚本额外依赖：bash、`python3`、`curl`；vllm 分支需安装对应版本的 `vllm` CLI。
