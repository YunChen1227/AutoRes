# AutoRes — 性能测试结果管理与报告 Agent

自动采集 **sglang / vllm / vllm-ascend** 的性能测试结果入库（SQLite），并通过 Web chatbot 用自然语言按需生成 Excel 对比报告。

完整设计见 [docs/design.md](docs/design.md)。

## 能做什么

| 能力 | 说明 |
|------|------|
| 自动入库 | Scanner 定时扫描 NAS 时间戳目录，解析 `result.csv` + `metadata.json` |
| 手工上传 | 数据不在 NAS 时，在 `/upload` 提交 CSV + 启动命令 txt + 元信息；支持 **单机/分布式** 与 **PD 分离** 两种部署模式 |
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
tools/vllm_sgl_benchs.sh ──► bench JSON 目录
                                    │
tools/to_csv.py ──────────► NAS 时间戳目录 ──► Scanner ──┐
frontend/upload.html ─────────────────────────► API 上传 ──┤──► SQLite
                                                           │
浏览器 chatbot / 下载 ◄── API + Agent + 报告流水线 ◄───────┘
```

- **压测脚本** `tools/vllm_sgl_benchs.sh`：按并发×输入长度矩阵批量跑 bench，并尽量抓取 KV hit rate / spec decoding 指标。
- **落盘脚本** `tools/to_csv.py`：把 bench JSON 整理为固定 schema 的 `result.csv` + `metadata.json`，写入 NAS 时间戳目录。
- **Scanner**（`autores/scanner/`）：定时扫描 NAS，解析入库。
- **API + 前端**（`autores/server/` + `frontend/`）：
  - `/` — chatbot（SSE）
  - `/upload` — 手工上传入库（含 PD 分离选项卡）
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
| `MODEL` / `TOKENIZER` | 见脚本 | 模型名与 tokenizer 路径 |
| `FLUSH_CACHE` | `0` | `1` = 每轮压测前清 server KV/prefix cache |

**输出：** 脚本内 `LOG_SUBDIR` 目录（默认 `tools/logs_910b_cjb_dsv4flashint8_8_260723/`）下多个 JSON。已存在的文件会跳过，可断点续跑。

**脚本内可改项：** `OUTPUT_LEN`、并发列表 `max_concurrency`、`input_length_map`（高并发时缩短输入长度）、`LOG_SUBDIR`、输出文件名前缀等。

**各框架 bench 命令摘要：**

| | sglang | vllm |
|---|--------|------|
| 命令 | `python3 -m sglang.bench_serving --backend sglang-oai-chat ...` | `vllm bench serve --backend openai ...` |
| KV hit rate | `--cache-report` → JSON 内 `cache_report.cache_hit_rate_pct` | 脚本前后抓 `/metrics`，注入 `kv_cache_hit_rate` |
| Spec | JSON 内 `accept_length` | JSON 内 `spec_decode_acceptance_rate` / `spec_decode_acceptance_length` |

### 2. 落盘：`tools/to_csv.py`

把上一步 JSON 目录转为 Scanner 可识别的 NAS 目录。

**sglang 示例：**

```bash
python tools/to_csv.py \
  --framework sglang --bench-framework sglang \
  --bench-flush-cache false \
  --framework-version 0.4.6 \
  --input-dir ./tools/logs_910b_cjb_dsv4flashint8_8_260723 \
  --nas-dir /mnt/nas/benchmark_root \
  --gpu-type H20-141G \
  --model DeepSeek-V4 \
  --launch-cmd "python -m sglang.launch_server --tp-size 8 --enable-cache-report --speculative-algorithm EAGLE"
```

> `--framework`（server 框架）与 `--bench-framework`（压测工具框架）**相互独立、均必填**，禁止默认一致——
> sglang bench 可打 vllm server，反之亦然，共 4 种组合。`--bench-flush-cache true/false` 记录压测前是否清缓存，
> 作为结果对比的区分维度入库（**必填**）。

**vllm 示例：**

```bash
python tools/to_csv.py \
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

> 压测脚本 `vllm_sgl_benchs.sh` 顶部把 `RUN_TO_CSV=1` 时，**压测跑完会自动调用一次 `to_csv.py`**
> （`--bench-flush-cache` 由脚本的 `FLUSH_CACHE` 派生，server/bench 框架用脚本内的两个变量），无需手动执行本步。

**vllm-ascend：** 与 vllm 相同，仅 `--framework vllm-ascend`；参数解析走 vllm 分支，入库 `framework` 仍存 `vllm-ascend`。

**要点：**

- `--input-dir`：压测 JSON 所在目录（sglang / vllm 均为 `*.json` 整文件解析）。
- `--framework` / `--bench-framework`：分别是 server 框架与压测工具框架，**均必填且相互独立**。
  `--bench-framework` 决定 bench JSON 字段解析（须与压测所用工具一致），`--framework` 决定 `--launch-cmd` 的参数提取。
- `--bench-flush-cache`：`true/false`，**必填**，记录压测前是否清缓存（flush=冷启动、不 flush=复用缓存，结果差异大，作为入库对比维度）。
- vllm / vllm-ascend 建议传 `--bench-cmd`，用于补 JSON 里没有的 `Input_Length`（从 `--random-input-len` 解析）。
- vllm bench 须含 `--percentile-metrics ttft,tpot,itl,e2el` 才有完整 E2E/ITL 列（压测脚本已包含）。
- `--launch-cmd`：完整服务启动命令；脚本提取 tp/dp/pp、投机解码、prefix caching 等入库维度，并计算 `gpu_count`。
- 成功后在 `--nas-dir` 下创建 `YYYYMMDD_HHMMSS/`，含 `result.csv` 与 `metadata.json`。

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

服务启动后打开 `http://<服务器>:8080/upload`：

1. 上传符合固定 schema 的结果 CSV（须含 `Input_Length`、`Concurrency`）——**选好文件后会自动按 spec 列识别 bench 框架**
2. 上传启动命令 txt（支持 `#` 注释、空行、反斜杠续行）
3. 选择 **单机/分布式** 或 **PD 分离**（检测到 disaggregation / kv-transfer 参数会自动切换）
4. 填写 `framework`（server 框架：`sglang` / `vllm` / `vllm-ascend`）、`framework_version`、`model`，选择 `gpu_type`
5. **bench 参数（必填）**：
   - **bench 框架**：与 server 框架相互独立。上传 CSV 后按 spec decoding 列是否有值自动预填
     （仅 vLLM 列有值→`vllm`，仅 SGLang 列有值→`sglang`；两者都有/都无则需手选），可手动改。
   - **是否 flush cache**：无法从 CSV 推断，**必须手动勾选**（flush=冷启动、不 flush=复用缓存）。

启动参数提取与 `to_csv.py` 同一套规则；PD 模式下分别填写 prefill / decode / router 启动命令。

#### 样例：`result.csv`（节选表头 + 一行）

完整表头以 `tools/to_csv.py` 的 `METRIC_FIELD_MAP` 为准；**必填列**只有 `Input_Length`、`Concurrency`，缺测填 `N/A`。

```csv
Input_Length,Concurrency,...,Completed,Total_Input_Tokens,Total_Output_Tokens,KV_Cache_Hit_Rate(%),SGLang_Spec_Accept_Length,vLLM_Spec_Accept_Rate(%),vLLM_Spec_Accept_Length
1024,32,...,400,409600,81920,63.5,2.87,N/A,N/A
```

sglang 行：`KV_Cache_Hit_Rate` + `SGLang_Spec_Accept_Length` 有值，vllm spec 列为 `N/A`。  
vllm 行反之；`KV_Cache_Hit_Rate(%)` 两边语义对齐（均为 0–100 的百分比）。

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
