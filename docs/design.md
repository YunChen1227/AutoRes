# 性能测试结果管理与报告 Agent — 总体架构设计（design.md）

| | |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-07-08 |
| 状态 | 架构定稿 / 待补充数据规范 |
| 上游文档 | `docs/性能测试结果管理与报告Agent-产品设计文档.pdf`（产品初稿 v0.1） |

本文档在产品初稿基础上，完成整体技术架构选型与各模块详细设计。所有关键决策均已与产品负责人逐项确认（见 §2 决策记录）。

---

## 1. 系统概述

系统由两条相互独立的链路组成，共享同一个 MongoDB 数据库（使用公司已有实例）：

1. **数据管道（自动、无人值守）**：定时扫描 NAS 上的测试结果目录，解析 `result.csv` + `metadata.json`，写入 MongoDB。
2. **报告服务（按需触发）**：Web 前端 chatbot 接收自然语言对比需求 → LLM Agent 多轮理解与澄清 → 确定性流水线查库、对齐、生成 Excel → 前端提供下载链接。

```
                          ┌─────────────────────────────────────────────┐         ┌──────────────┐
  测试人员                 │              内网服务器 (Docker Compose)      │  写入    │  MongoDB     │
     │ 落盘                │                                             │  ┌─────►│ (公司已有实例) │
     ▼                    │  ┌───────────┐                              │  │      │ test_runs    │
 ┌────────┐   目录挂载     │  │  Scanner  │──────────────────────────────┼──┘      │ ingest_log   │
 │  NAS   │◄──────────────┼──┤ (进程 A)  │                              │         └──────┬───────┘
 └────────┘               │  └───────────┘                              │   查询          │
                          │  ┌────────────────────────────────────────┐ │◄───────────────┘
  产品/项目同事             │  │        API Server (进程 B, FastAPI)    │ │
     │ 浏览器              │  │  ┌──────────┐  ┌─────────┐  ┌───────┐ │ │
     ▼                    │  │  │ 静态前端  │  │ Agent   │  │ 报告   │ │ │
 ┌──────────┐  HTTP/SSE   │  │  │ (单文件)  │  │ 工具循环 │  │ 流水线 │ │ │
 │ Web 前端  │◄────────────┼──┤  └──────────┘  └────┬────┘  └───┬───┘ │ │
 │ chatbot  │             │  └────────────────────┼───────────┼─────┘ │
 └──────────┘             │                       │           │       │
                          └───────────────────────┼───────────┼───────┘
                                                  ▼           ▼
                                          OpenAI 兼容      临时报告目录
                                          LLM 端点         (TTL 清理)
```

---

## 2. 架构决策记录（ADR）

以下决策已逐项讨论确认，是本设计的边界条件：

| # | 决策点 | 结论 | 理由 / 备注 |
|---|--------|------|------------|
| D1 | 交互入口 | **自建 Web 前端 chatbot**（暂不接入办公 IM） | 办公软件暂无法接入；Excel 通过前端下载链接获取。接口层预留未来接 IM 的扩展位（见 §10.3） |
| D2 | NLU 方案 | **外部 OpenAI 兼容 LLM 端点**，地址/密钥由使用方在 config 文件中填写 | 端点支持 function calling；系统不绑定任何具体模型 |
| D3 | 数据库 | **MongoDB**（文档型，一次测试=一个文档，metrics 内嵌数组）；**使用公司已有实例**，部署仅填连接串 | 见 §6；替代早期 PostgreSQL 方案 |
| D4 | 部署环境 | **内网 Linux 服务器 + NAS 目录挂载** | Scanner 以本地文件系统方式读 NAS 挂载点 |
| D5 | 后端技术栈 | **Python + FastAPI** | pandas 解析 CSV、openpyxl 生成 Excel、PyMongo/Motor 访问 MongoDB，生态匹配 |
| D6 | 前端形态 | **原生 HTML/JS 单文件 SPA**，由 FastAPI 静态托管 | 不引入构建链，一个 `index.html` 搞定；单服务部署 |
| D7 | 交互模式 | **多轮对话 + 主动澄清** | 需求有歧义（如缺版本号）时 Agent 反问并列出候选，而非猜测 |
| D8 | Agent 架构 | **混合模式**：需求理解/澄清/数据定位用 LLM 工具循环；确定查询规格后，Excel 生成走确定性代码流水线 | LLM 负责"理解"，代码负责"产出"；报告内容不经过 LLM，保证数据零失真 |
| D9 | 进程形态 | **同一代码库、两个进程**（Scanner + API Server），docker-compose 编排 | 扫描异常不影响问答服务，互相隔离 |
| D10 | 数据规范 | **由落盘脚本硬编码保证**：改造后的 `to_csv.py` 生成固定结构的 `result.csv` + `metadata.json`（详见 §5）。解析器面向该固定 schema，不再需要可配置字段映射 | 见 §5、§6 |
| D11 | 认证 | **无认证** | 纯内网环境，信任网络边界 |
| D12 | 报告内容 | **纯数据对比表**，不含 LLM 结论、不含图表、不含元信息 sheet | 同事拿到后自行二次加工 |
| D13 | 持久化 | **会话与报告均不持久化**：会话仅存内存（带 TTL），报告文件临时存盘供一次性下载、定期清理 | 数据库只承载测试数据本身 |
| D14 | 字段归一化 | **入库保留原始值，查询时由 LLM 模糊对齐**：Agent 先用工具拉取库内实际维度值，再让 LLM 将用户口语（如"4090"）对齐到库内值（如"NVIDIA RTX 4090"） | 零人工词表维护成本；对齐结果在澄清环节可被用户确认 |
| D15 | 多框架支持 | 落盘脚本 `to_csv.py` 新增 `--framework {sglang,vllm}` 入参；两框架 bench 输出字段名不同，脚本内做字段映射统一为同一套 metric 名 | 见 §5.1、§5.2 |
| D16 | 元信息落盘 | `metadata.txt` 改为 `metadata.json`（结构化、便于入库）；测试人员通过入参提供 **NAS 地址、显卡类型、启动框架、完整启动命令字符串** | JSON 比 kv-txt 更适合承载嵌套的结构化启动参数，见 §5.3 |
| D17 | 启动参数提取 | 在**落盘脚本侧**从完整启动命令字符串提取并行度与开关（tp/dp/pp/ep/cp、hicache、flexkv、kv-cache-dtype 等），结构化写入 `metadata.json`；提取规则内置框架默认值，命令未显式写的参数按框架默认回填 | 规则集中一处、入库端零框架知识，见 §5.4 |
| D18 | 参数入库形态 | **折中**：并行度（tp/dp/pp/ep/cp）+ 核心通用开关（kv_cache_dtype、hicache_enabled、flexkv_enabled、torch_compile、quantization、attention_backend）平铺进文档 `params` 子对象（可直接 filter/建索引）；框架专属细节（hicache_ratio 等）进 `extra` 子对象 | 高频对比维度可直接筛选，避免文档字段因框架特性膨胀，见 §6 |
| D19 | 半成品防护 | **取消完成标记机制**：csv/json 均由脚本硬编码原子生成，不存在半成品。入库解析失败即**不记录该 timestamp 目录**（下轮自动重试），成功才记入台账 | 简化 §5.5（原 §5.2 完成标记方案作废） |
| D20 | 取数策略 | **所有维度全同才取最新一次**；框架版本不同（如 vllm 0.5.11 vs 0.5.12）视为不同记录**全部取出**；不同框架（vllm vs sglang）的版本号**不可跨框架比较**，各自独立取 | 修订原 §7.2 的"latest"策略，见 §7.4 |
| D21 | 排除逻辑 | 取出数据可能过多，Agent 除"取哪些"外还支持"排除哪些"：QuerySpec 增加 `exclude` 字段，用户可要求剔除某维度值（如"去掉 A800 的"） | 见 §7.3、§7.4 |

---

## 3. 技术栈清单

| 层 | 选型 | 用途 |
|----|------|------|
| 语言 | Python ≥ 3.11 | 全部后端逻辑 |
| Web 框架 | FastAPI + uvicorn | REST API、SSE 流式回复、静态文件托管 |
| DB 驱动 | PyMongo（同步） | MongoDB 访问；单节点、数据量小、无并发压力，Scanner 与 API 统一用同步 PyMongo（API 侧查询放线程池，避免阻塞事件循环），不引入 Motor 增加复杂度 |
| CSV 解析 | pandas | result.csv 读取与清洗 |
| Excel 生成 | openpyxl | 对比报告渲染 |
| LLM 客户端 | openai 官方 SDK（指向自定义 base_url） | 任何 OpenAI 兼容端点均可用 |
| 调度 | Scanner 进程内 `while + sleep` 循环（简单可靠） | 定时扫描；不依赖系统 cron |
| 配置 | YAML（`config.yaml`）+ 环境变量覆盖 | 见 §9 |
| 前端 | 原生 HTML/CSS/JS 单文件 | chatbot 界面 |
| 部署 | Docker + docker-compose | scanner / api 两个容器（Mongo 用公司已有实例，不入 compose） |

---

## 4. 代码库结构（规划）

```
AutoRes/
├── config.example.yaml        # 配置模板（拷贝为 config.yaml 后填写）
├── docker-compose.yml
├── Dockerfile
├── tools/
│   └── to_csv.py              # 测试人员本地运行的落盘脚本（生成 timestamp 目录 + result.csv + metadata.json）
├── docs/
│   ├── design.md              # 本文档
│   └── 性能测试结果管理与报告Agent-产品设计文档.pdf
├── frontend/
│   └── index.html             # 单文件 SPA，由 FastAPI 挂载为静态资源
└── autores/                   # Python 包
    ├── config.py              # 配置加载（YAML + env 覆盖）
    ├── db/
    │   ├── client.py          # MongoDB 连接（PyMongo 同步）、集合句柄、索引初始化
    │   └── schema.py          # 文档结构定义与校验（test_runs / ingest_log）
    ├── scanner/               # 进程 A
    │   ├── main.py            # 轮询主循环入口
    │   ├── discovery.py       # 新目录发现（timestamp 目录识别 + 已处理台账比对）
    │   └── parser.py          # 读取 result.csv + metadata.json，写入数据库
    ├── server/                # 进程 B
    │   ├── main.py            # FastAPI 入口
    │   ├── api.py             # /api/chat、/api/download 等路由
    │   ├── session.py         # 内存会话管理（TTL）
    │   ├── agent/
    │   │   ├── loop.py        # LLM 工具循环
    │   │   ├── tools.py       # 工具定义与实现
    │   │   └── prompts.py     # system prompt
    │   └── report/
    │       ├── query.py       # QuerySpec → MongoDB 查询/聚合
    │       ├── align.py       # 数据对齐（内嵌指标 → 对比宽表）
    │       └── excel.py       # openpyxl 渲染
    └── common/
        └── logging.py
```

---

## 5. 数据采集与落盘（to_csv.py + Scanner）

数据链路的源头是测试人员在本机运行的落盘脚本 `to_csv.py`（改造自现有脚本），它把 bench 原始输出整理成**固定 schema** 的 `result.csv` + `metadata.json`，写入 NAS 上以时间戳命名的目录。Scanner 只面向这套固定 schema 解析，因此不再需要可配置字段映射（D10）。

### 5.1 落盘脚本 to_csv.py：入参

改造后的脚本新增以下入参（D15/D16）：

| 入参 | 必填 | 说明 |
|------|------|------|
| `--framework {sglang,vllm}` | 是 | 决定按哪套字段名解析 bench 输出（两框架字段名不同，见 §5.2） |
| `--input-dir` | 是 | bench 原始输出所在目录（sglang 为 JSONL，vllm 为多个 JSON） |
| `--nas-dir` | 是 | NAS 挂载根路径（各测试人员挂载位置不同，由入参指定），脚本在其下创建时间戳目录 |
| `--gpu-type` | 是 | 显卡类型（如 `H20-141G`），写入 metadata.json |
| `--model` / `--model-version` | 是 | 模型名与版本 |
| `--launch-cmd` | 是 | **完整服务启动命令字符串**（如 `"python -m sglang.launch_server --tp-size 8 --enable-hierarchical-cache ..."`）；脚本据此提取结构化启动参数（见 §5.4） |
| `--bench-cmd` | 否 | 完整 benchmark 命令字符串；vllm 场景用于补 `random_input_len` 等 bench 侧信息（见 §5.2 注） |

脚本输出目录结构：

```
{nas_dir}/
└── 20260708_143000/          # 脚本按落盘时刻生成的时间戳目录（唯一标识）
    ├── result.csv            # 固定列头的量化指标表
    └── metadata.json         # 结构化元信息 + 启动参数
```

### 5.2 result.csv：两框架字段映射

脚本把两框架 bench 输出的字段名统一映射为**同一套 metric 列**。以下映射基于对 vllm/sglang 最新 main 分支源码的核查：

| 统一 metric 列 | sglang JSON key | vllm JSON key | 备注 |
|----------------|-----------------|---------------|------|
| Input_Length | `random_input_len` | *(无，取自 `--bench-cmd` 的 `--random-input-len`)* | vllm bench JSON 不含输入长度 |
| Concurrency | `max_concurrency` | `max_concurrency` | 一致 |
| Request_Throughput | `request_throughput` | `request_throughput` | 一致 |
| Input_Throughput | `input_throughput` | *(vllm 无此字段)* | vllm 无 input 侧吞吐，填 N/A |
| Output_Throughput | `output_throughput` | `output_throughput` | 一致 |
| Total_Throughput | `total_throughput` | `total_token_throughput` | **名称不同** |
| TTFT_{Mean,Median,P99}(ms) | `{mean,median,p99}_ttft_ms` | 同 | 一致 |
| TPOT_{Mean,Median,P99}(ms) | `{mean,median,p99}_tpot_ms` | 同 | 一致 |
| ITL_{Mean,Median,P99}(ms) | `{mean,median,p99}_itl_ms` | 同 | 一致 |
| E2E_{Mean,Median,P99}(ms) | `{mean,median,p99}_e2e_latency_ms` | `{mean,median,p99}_e2el_ms` | **名称不同**（vllm 是 `e2el`） |

新增建议指标（§5.2 之外、两框架都有、对性能分析有价值，纳入 CSV）：`Completed`（成功请求数）、`Total_Input_Tokens`、`Total_Output_Tokens`、`Duration_s`、`P95` 分位（sglang 原生有；vllm 需 bench 时配 `--metric-percentiles`）。

> **vllm 落盘注意（写入文档供测试人员遵循）**：vllm bench 默认**不生成 e2el 统计**，且输入长度不进 JSON。测试人员跑 vllm bench 时需：① 加 `--percentile-metrics ttft,tpot,itl,e2el` 才有 E2E 指标；② 通过 `--bench-cmd` 把完整 bench 命令传给脚本，脚本从中解析 `--random-input-len`。缺失字段一律填 `N/A`，不阻塞落盘。
>
> **sglang 落盘注意**：sglang bench 输出为 **JSONL**（每行一次 run），脚本按行解析；vllm 为每次 run 一个独立 JSON 文件。

### 5.3 metadata.json：结构

```jsonc
{
  "framework": "sglang",
  "framework_version": "0.4.6",         // 由 --framework-version 入参手动传入（P4 已定）
  "model": "GLM-4.5",
  "model_version": "distributed2",
  "gpu_type": "H20-141G",
  "launch_cmd": "python -m sglang.launch_server --tp-size 8 --enable-hierarchical-cache ...",  // 原文保留
  "params": {                           // §5.4 提取规则的产物，结构化（入库时平铺进文档 params，§6.1）
    "tp": 8, "dp": 1, "pp": 1, "ep": 1, "cp": 1,
    "kv_cache_dtype": "auto",
    "hicache_enabled": true,
    "flexkv_enabled": false,
    "torch_compile": false,
    "quantization": null,
    "attention_backend": null
  },
  "extra": {                            // 框架专属细节，入库进文档 extra 子对象
    "hicache_ratio": 2.0,
    "hicache_write_policy": "write_through"
  }
}
```

`launch_cmd` 原文始终保留，作为提取结果的溯源与人工复核依据。

### 5.4 启动参数提取规则（脚本内置，D17）

脚本从 `--launch-cmd` 字符串解析出结构化并行度与开关。核心要点：

- **多别名匹配**：同一参数在两框架有多个等价写法，规则表需覆盖全部别名。例如 tp：sglang `--tp-size` / `--tensor-parallel-size`（无短别名）；vllm `--tensor-parallel-size` / `-tp`。ep：sglang `--ep-size` / `--ep` / `--expert-parallel-size`（数值）；vllm `--enable-expert-parallel` / `-ep`（**布尔开关**，度由 TP×DP 派生，提取时置 `ep_enabled=true` 而非数值）。
- **CP 的框架差异**：sglang 无单一 `--cp-size`，拆为 `--dcp-size`（decode）与 `--attn-cp-size`（attention）+ `--enable-prefill-cp`；vllm 为 `-dcp`/`--decode-context-parallel-size` 与 `-pcp`/`--prefill-context-parallel-size`。规则按框架分别提取，统一归一到 `cp` 概念（取 dcp 值为主，细节进 extra）。
- **hicache（分层缓存）**：sglang `--enable-hierarchical-cache` 置 `hicache_enabled=true`，`--hicache-ratio`/`--hicache-write-policy`/`--hicache-io-backend`/`--hicache-storage-backend` 进 `extra`；vllm **无 hicache 概念**，对应 `--kv-offloading-size`/`--kv-offloading-backend`，若出现则 `hicache_enabled=true` 并把 backend 记入 extra。
- **flexkv**：sglang `--enable-flexkv` → `flexkv_enabled=true`；vllm 走 `--kv-transfer-config` JSON，若其 `kv_connector` 为 `FlexKVConnectorV1` 则 `flexkv_enabled=true`。
- **默认值回填（D-默认值）**：命令未显式写的参数按**框架默认值**回填（§5.4.1 默认值表），确保 tp=1 与"未写 tp"在库里语义一致，避免对比时出现虚假差异。
- 无法识别的 flag 原样收集进 `extra`，不丢弃。

#### 5.4.1 框架默认值表

下表默认值均经 vllm/sglang 最新 main 分支**源码逐字核查**（附出处），脚本内置为常量；随框架大版本演进需复核。

| 统一字段 | sglang 默认 | sglang 出处 | vllm 默认 | vllm 出处 |
|----------|-------------|-------------|-----------|-----------|
| tp | 1 | server_args.py:816 | 1 | config/parallel.py:122 |
| dp | 1 | server_args.py:842 | 1 | config/parallel.py:126 |
| pp | 1 | server_args.py:830 | 1 | config/parallel.py:120 |
| ep | 1（size 语义） | server_args.py:1784 | false（开关语义，`enable_expert_parallel`） | config/parallel.py:162 |
| cp（取 dcp） | 1 | server_args.py:823 | 1（decode_context_parallel_size） | config/parallel.py:339 |
| kv_cache_dtype | `auto` | server_args.py:558 | `auto` | cache.py:76 |
| hicache_enabled | false | server_args.py:2010 | false（无此概念；映射 `--kv-offloading-*`，默认关） | cache.py:182 |
| flexkv_enabled | false | server_args.py:2252 | false（`kv_connector` 未含 FlexKV 即 false） | — |
| prefix caching | 启用（`disable_radix_cache` 默认 False） | server_args.py:755 | 启用（CacheConfig `enable_prefix_caching` 默认 True） | cache.py:93 |
| quantization | null | server_args.py:538 | null | model.py:203 |
| torch_compile | false（`enable_torch_compile`） | server_args.py:1583 | 默认启用（`enforce_eager` 默认 False）；提取时记 `torch_compile=true` | model.py:215 |
| attention_backend | null（按硬件自动选） | server_args.py:1295 | *(vllm 无对等单一 flag，留空)* | — |

> **hicache 细节默认值**（进 `extra`，仅 sglang）：`hicache_ratio=2.0`、`hicache_write_policy=write_through`、`hicache_io_backend=kernel`、`hicache_storage_backend=None`（server_args.py:2011/2019/2026/2046）。
>
> **注意 torch_compile 的框架差异**：sglang 默认关（需显式 `--enable-torch-compile`），vllm 默认开（除非 `--enforce-eager`）。对比时这是真实差异，脚本按各自默认如实回填。
>
> **注意 chunked_prefill / mem_fraction 等**：sglang 中 `chunked_prefill_size`、`mem_fraction_static`、`attention_backend` 默认均为 None（运行时按 GPU 显存自动计算），无法回填确定值，命令未写时在 extra 里记 `"auto"`。

### 5.5 Scanner：扫描、入库与半成品处理（D19）

- **扫描主循环**：按 `scanner.interval_seconds`（默认 300s）轮询 `scanner.benchmark_root`（NAS 挂载点）；列出一级子目录 → 过滤时间戳格式目录 → 与已成功台账比对 → 处理未入库目录。
- **无半成品概念**：csv/json 由脚本硬编码原子生成，不存在"写一半"。因此**取消完成标记文件、静默期等机制**。
- **失败即不记录**：解析/入库失败的目录**不写入成功台账**，仅打日志；下一轮扫描自然重试，直到成功才记账。无需 retry_count / abandoned 状态，无需人工干预流程。
- **幂等**：`test_runs._id`（= 目录名）唯一 + "只处理不在 ingest_log 台账中的目录"双重保证每目录仅入库一次；重复插入因 `_id` 冲突被 Mongo 拒绝。
- **写入原子性**：单目录的整份 `test_runs` 文档（含内嵌 metrics 数组）一次 `insert_one` 写入，本身即原子；随后写 `ingest_log`。若中途失败，因 `_id` 幂等，下轮重试安全（详见 §6.2）。

---

## 6. 数据模型（MongoDB）

两个集合。`test_runs` 每个文档 = 一次完整测试（一张 result.csv + 其 metadata.json，D18/方案 A：一次测试=一个文档，指标内嵌数组）；`ingest_log` 是**已入库目录台账**，用于区分哪些 timestamp 目录已处理、哪些未处理。

### 6.1 `test_runs` 集合

```jsonc
{
  "_id": "20260708_143000",            // 直接用 timestamp 目录名作主键（天然唯一，即 source_dir）
  "run_timestamp": ISODate("2026-07-08T14:30:00"),  // 由目录名解析

  // ── 元信息维度 ──
  "model": "GLM-4.5",
  "model_version": "distributed2",
  "framework": "sglang",               // sglang | vllm
  "framework_version": "0.4.6",        // 落盘入参手动传（P4 已定）
  "gpu_type": "H20-141G",              // 来自 --gpu-type
  "launch_cmd": "python -m sglang.launch_server --tp-size 8 ...",  // 原文，溯源

  // ── 结构化启动参数（D18：高频对比维度平铺为顶层字段，可直接 filter）──
  "params": {
    "tp": 8, "dp": 1, "pp": 1, "ep": 1, "cp": 1,
    "kv_cache_dtype": "auto",
    "hicache_enabled": true,
    "flexkv_enabled": false,
    "torch_compile": false,
    "quantization": null,
    "attention_backend": null
  },

  // ── 框架专属细节 + 未识别参数（对应原 JSONB）──
  "extra": {
    "hicache_ratio": 2.0,
    "hicache_write_policy": "write_through"
  },

  // ── 指标内嵌数组（方案 A：每个 (输入长度,并发) 组合一个元素）──
  "metrics": [
    {
      "input_length": 1024, "concurrency": 32,
      "Request_Throughput": 12.5, "Output_Throughput": 3200.0, "Total_Throughput": 4100.0,
      "Input_Throughput": 900.0,
      "TTFT_Mean_ms": 85.2, "TTFT_Median_ms": 80.1, "TTFT_P95_ms": 120.3, "TTFT_P99_ms": 140.0,
      "TPOT_Mean_ms": 15.1, "TPOT_Median_ms": 14.8, "TPOT_P95_ms": 18.0, "TPOT_P99_ms": 20.2,
      "ITL_Mean_ms": 14.9, "ITL_Median_ms": 14.5, "ITL_P95_ms": 17.5, "ITL_P99_ms": 19.8,
      "E2E_Mean_ms": 2100.0, "E2E_Median_ms": 2050.0, "E2E_P95_ms": 2400.0, "E2E_P99_ms": 2600.0,
      "Completed": 500, "Total_Input_Tokens": 512000, "Total_Output_Tokens": 128000
    }
    // ... 每个输入长度×并发组合一条
  ],

  "created_at": ISODate("2026-07-08T14:35:12")
}
```

**索引**（`db/client.py` 启动时确保）：

- `_id` 天然唯一（= 目录名），承担幂等去重职责，无需额外唯一约束。
- 复合索引 `{model:1, framework:1, framework_version:1, gpu_type:1}` —— 元信息维度筛选主路径。
- 复合索引 `{"params.tp":1, "params.dp":1, "params.pp":1, "params.ep":1, "params.cp":1}` —— 并行度对比。
- 指标筛选（input_length/concurrency）走内嵌数组，查询时用 `$elemMatch` / 聚合 `$unwind`，量级小不建额外索引。

### 6.2 `ingest_log` 集合（已入库/未入库追踪，D19）

这是你问的"哪些目录已入库、哪些未入库"的核心机制：

```jsonc
{
  "_id": "20260708_143000",            // = timestamp 目录名
  "ingested_at": ISODate("2026-07-08T14:35:12")
}
```

**判定逻辑**（§5.5 Scanner 每轮执行）：

1. 列出 NAS 根目录下所有时间戳格式子目录，得到集合 `on_disk`。
2. 查 `ingest_log` 所有 `_id`，得到已入库集合 `ingested`。
3. **待处理 = `on_disk` − `ingested`**，逐个解析入库。
4. 单目录成功后，**先写 `test_runs` 文档，再写 `ingest_log` 记录**（单节点无事务，D-P6：不用多文档事务）。两步之间即便崩溃也安全——`test_runs._id` 与 `ingest_log._id` 都是目录名，重跑时 `insert_one` 因 `_id` 冲突被拒，不会产生重复数据。
5. **失败即不写 ingest_log**（D19）——该目录下轮扫描自然重新出现在"待处理"集合里，自动重试。因此 `ingest_log` 里存在 = 已成功入库，不存在 = 未入库（含从未处理和处理失败两种，行为上都会被下轮重试）。

> **崩溃边界**：若"写完 test_runs、还没写 ingest_log"时崩溃，下轮该目录仍被视为未入库、重新处理；此时 `test_runs` 的 `insert_one` 因 `_id` 已存在而报 DuplicateKey，Scanner 捕获该异常视为"已入库"，补写 ingest_log 即可。逻辑简单且无需事务。

> 为什么单列 `ingest_log` 而不直接查 `test_runs` 是否有该 `_id`？两者 `_id` 一致，理论上查 `test_runs` 也能判定。单列 `ingest_log` 的好处：① 台账极轻量（只有目录名+时间），扫描比对快；② 语义清晰，未来若要记录"处理但主动跳过"（如空目录）可扩展字段而不污染数据集合。**若想极简，也可省去 ingest_log，直接以 `test_runs` 是否含该 `_id` 判定**——这是可选的简化，见 §12 P6。

**几点说明**：

- `_id` 用目录名，既是主键又是幂等键，重复入库因 `_id` 冲突而被 Mongo 拒绝（`insert` 报 DuplicateKey），天然防重。
- `gpu_type` 合并了原设计的 gpu_model + gpu_version（落盘只收单一 `--gpu-type` 字段）。
- 会话与报告不持久化（D13），因此**没有** conversation / report 集合。

---

## 7. 报告服务设计（API Server，进程 B）

### 7.1 混合架构总览（D8）

```
用户消息 ──► [阶段一：LLM 工具循环]                [阶段二：确定性流水线]
             理解意图、拉取库内维度值、             QuerySpec ─► MongoDB 查询/聚合
             模糊对齐、发现歧义则反问用户、    ──►   ─► 数据对齐（内嵌指标→宽表）
             最终产出一份结构化 QuerySpec           ─► openpyxl 渲染 Excel
                                                   ─► 返回下载链接
```

关键原则：**LLM 只产出"查什么"（QuerySpec），永远不接触"查到的数"**。指标数值从 MongoDB 到 Excel 全程由代码搬运，杜绝 LLM 幻觉污染报告数据。

### 7.2 阶段一：LLM 工具循环

标准 function-calling 循环：把用户消息 + 会话历史发给 LLM，LLM 或者回复文本（澄清/闲聊），或者调用工具；工具结果回填后继续循环，直到 LLM 调用 `submit_query_spec` 或输出纯文本回复。单轮循环上限 `llm.max_tool_rounds`（默认 8）防失控。

**工具清单**（3 个，刻意保持最小集）：

| 工具 | 入参 | 返回 | 用途 |
|------|------|------|------|
| `list_dimension_values` | `dimension`（枚举：model / model_version / framework / framework_version / gpu_type / tp / dp / pp / ep / cp / kv_cache_dtype / hicache_enabled / flexkv_enabled / metric_name），可选 `filters`（其他维度的等值约束） | 库内该维度的去重值列表 + 各值的记录数 | 归一化对齐（D14）：用户说"4090"，LLM 拉取 gpu_type 实际值后自行对齐到"NVIDIA RTX 4090"；也用于澄清时向用户列候选 |
| `count_matching_runs` | 一组维度等值/排除条件 | 命中的文档数量 + 若数量 ≤ 20 则附简要清单（目录名、时间戳、各维度值） | 提交前预检：0 条 → 告知用户没有该数据；数量过多 → 提示用户可加约束或排除某维度值（D21） |
| `submit_query_spec` | 完整 QuerySpec（见 7.3） | 校验结果；通过则触发阶段二 | 工具循环的**唯一出口**；后端对 spec 做严格 schema 校验，非法则把错误回给 LLM 修正 |

**System prompt 要点**（`prompts.py`，设计要求而非全文）：

- 角色：性能测试数据查询助手，任务是把对比需求转化为 QuerySpec。
- 明确"对比轴（compare_on）"与"约束项（filters）"的概念，对应初稿 §6。
- 强制行为规则：
  1. 对齐任何用户提到的维度值之前，**必须**先调 `list_dimension_values` 确认库内真实值，禁止凭空猜测拼写；
  2. 出现歧义（一个口语值匹配多个库内值 / 用户漏说必要约束导致对比不成立）时，**必须**向用户反问并列出候选项，禁止擅自选择（D7）；
  3. 提交前**必须**先 `count_matching_runs` 预检；
  4. **取数策略（D20）**：只有当一组记录的**所有维度完全相同**时才取最新一次；框架版本不同（如 vllm 0.5.11 与 0.5.12）视为不同记录，**全部取出**，不做去重；**不同框架**（vllm 与 sglang）的版本号**不可跨框架比较**，须各自独立呈现。向用户说明这一行为。
  5. **排除逻辑（D21）**：当结果过多、或用户明确要求"去掉某某"时，用 QuerySpec 的 `exclude` 字段剔除指定维度值，而非重新构造复杂 filter。
- 回复语言与用户一致（默认中文）。

### 7.3 QuerySpec（两阶段之间的契约）

```jsonc
{
  "compare_on": "gpu_type",            // 对比轴：横向比较的维度（单选）
  "filters": {                          // 约束项：其余维度的固定条件（等值，多值用数组）
    "model": "GLM-4.5",
    "framework": "sglang",
    "tp": 8
  },
  "compare_values": ["H20-141G", "H800"],   // 对比轴上的目标值（可选；缺省=该轴下所有匹配值）
  "exclude": {                          // 排除项（D21）：从结果中剔除指定维度值
    "gpu_type": ["H20-96G"]             // 如"去掉 H20-96G 的"
  },
  "metrics": ["Output_Throughput", "TTFT_Mean(ms)"],  // 要对比的指标（可选；缺省=全部）
  "metric_filters": {                   // 可选：按输入长度/并发进一步筛选（见 §6 建模）
    "input_length": [1024], "concurrency": [32, 64]
  }
}
```

**取数策略固定为 D20 语义**（不再有 `run_selection` 选项）：查询后按"除 run_timestamp 外所有维度"分组，**每组内取最新一次**；不同 framework_version、不同 framework 天然落在不同分组，因此自动全部保留、不跨框架去重。

后端以 JSON Schema 严格校验（枚举合法维度名、compare_on 不得同时出现在 filters/exclude、exclude 值必须是库内存在值等）；校验失败原样反馈给 LLM 让其自我修正，最多重试 2 次。

### 7.4 阶段二：确定性报告流水线

1. **查询**：QuerySpec → MongoDB `find`/聚合，取出匹配的 `test_runs` 文档（含内嵌 metrics）。filters/compare_values/exclude 转为 `$match` 条件；metric_filters（输入长度/并发）用 `$unwind` + `$match` 展开内嵌数组后筛选。
2. **取最新（D20）**：结果按"除时间戳外所有维度组合"分组，每组取 `run_timestamp` 最大者。**不同框架版本、不同框架各自成组，全部保留**——不会因版本不同被误合并。
3. **对齐**：把各文档的内嵌指标透视为宽表 —— **行 = 指标（含输入长度/并发维度），列 = 对比轴的各个取值**；某组合缺某指标时该单元格填 `N/A`（不留空，明确表达"无数据"）。
4. **渲染**（纯数据对比表，D12）：单个 sheet；首行标题区注明约束项（如"模型: GLM-4.5 | 框架: sglang | TP: 8"），随后是对比表；仅做基础可读性格式（表头加粗、冻结首行首列、列宽自适应），不加图表、不加结论。
5. **产出**：文件写入 `report.output_dir`，文件名 `对比报告_{compare_on}_{时间戳}.xlsx`；生成随机下载 token（UUID），token→文件路径映射存内存；聊天回复中附下载链接及本次取数摘要（几条记录、哪些组合、是否有跨框架/多版本、是否有 N/A）。

### 7.5 会话管理（不持久化，D13）

- 会话存进程内存：`session_id → {messages: [...], created_at, last_active}`。
- 前端首次加载时生成 `session_id`（UUID）存 sessionStorage，随每条消息提交。
- 过期策略：`session.ttl_minutes`（默认 60）无活动即回收；服务重启会话清零（可接受，用户重新描述即可）。
- 上下文长度控制：会话消息超过 `session.max_messages`（默认 40）时截断最早消息。

### 7.6 报告文件生命周期

- 报告写入本地临时目录（非 NAS）；后台任务每 10 分钟清理生成时间超过 `report.ttl_minutes`（默认 120）的文件及其 token。
- 下载链接失效后访问返回 410，前端提示"报告已过期，请重新生成"。

---

## 8. API 设计

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 入参 `{session_id, message}`；返回 **SSE 流**，事件类型见下 |
| GET | `/api/download/{token}` | 下载 Excel；`Content-Disposition: attachment`；过期返回 410 |
| GET | `/api/health` | 健康检查：DB 连通性 + LLM 端点可达性 + 最近一次扫描时间 |
| GET | `/` | 返回 `frontend/index.html` |

**SSE 事件类型**（前端据此渲染）：

- `status`：过程提示（如"正在查询库内显卡型号…"），对应 Agent 每次工具调用，让用户看到进展；
- `message`：LLM 的文本回复（澄清反问或最终答复），按 token 流式推送；
- `report`：`{download_url, filename, summary}`，前端渲染为下载卡片；
- `error`：错误提示（LLM 端点超时、数据库异常等），前端以红色气泡展示；
- `done`：本轮结束。

---

## 9. 配置文件设计（config.yaml）

使用方拷贝 `config.example.yaml` 为 `config.yaml` 填写。所有项均可用环境变量 `AUTORES_<段>_<键>`（如 `AUTORES_LLM_API_KEY`）覆盖，便于不把密钥写进文件。

```yaml
llm:                                  # ← 使用方重点填写（D2）
  base_url: "http://your-llm-host:8000/v1"   # OpenAI 兼容端点
  api_key: "sk-xxxx"                          # 无鉴权端点可填任意占位
  model: "your-model-name"
  temperature: 0.1                            # 意图解析要低温
  timeout_seconds: 60
  max_tool_rounds: 8

database:                             # ← 公司已有 MongoDB 实例，填连接串即可（D3）
  uri: "mongodb://user:pass@your-mongo-host:27017/?authSource=admin"
  db_name: "autores"
  # 集合名固定：test_runs、ingest_log（代码内常量，无需配置）

scanner:
  benchmark_root: "/mnt/nas/benchmark_root"   # NAS 挂载点（对应测试人员落盘的 --nas-dir）
  interval_seconds: 300
  dir_pattern: '^\d{8}_\d{6}$'                # 时间戳目录名正则
  # 无 done_marker / retry 配置：csv/json 原子生成，失败即不记录、下轮自动重试（D19）

server:
  host: "0.0.0.0"
  port: 8080

session:
  ttl_minutes: 60
  max_messages: 40

report:
  output_dir: "/data/reports"
  ttl_minutes: 120
```

---

## 10. 部署设计

### 10.1 docker-compose 编排（D9）

**两个服务**（MongoDB 用公司已有实例，不在 compose 内起容器，D3）：

| 服务 | 镜像 | 说明 |
|------|------|------|
| `scanner` | 项目镜像，入口 `python -m autores.scanner.main` | 挂载 NAS 目录（只读）+ config.yaml；连公司 Mongo |
| `api` | 同一项目镜像，入口 `uvicorn autores.server.main:app` | 暴露 8080；挂载 config.yaml + 报告临时卷；连公司 Mongo |

scanner 与 api 用同一 Dockerfile 构建的同一镜像，仅入口命令不同（D9：同库两进程）。MongoDB 无需建表，两进程启动时各自 `create_index`（幂等）确保 §6.1 的索引存在即可。前置要求：公司 Mongo 实例需从本服务器网络可达，并预先创建好 `autores` 库与读写账号。

### 10.2 运行要求

- 服务器需将 NAS 挂载为本地路径（如 `/mnt/nas/benchmark_root`），再以只读方式挂进 scanner 容器 —— Agent 永远不写 NAS。
- LLM 端点需从该服务器可达（内网或经代理）。

### 10.3 预留扩展位

以下不在本期范围，但架构上已留好接口，未来接入不动核心：

- **IM 接入（未来重新评估 D1 时）**：报告服务的核心入口是"一条消息进、若干事件出"，`/api/chat` 之外可平行增加 IM webhook 适配器，复用同一 Agent 循环与流水线；
- **认证**：FastAPI 中间件位；
- **归一化词表**：若 LLM 对齐准确率不达预期，可在配置中追加"别名→标准名"映射，升级为"配置基础归一 + LLM 兜底"的组合策略（D14 的备选路线）；
- **新框架接入**：新增 TensorRT-LLM 等框架时，只需在 `to_csv.py` 增加一套字段映射 + 默认值表 + 启动参数提取规则（§5），入库端与报告端不动。

---

## 11. 错误处理与可观测性

| 场景 | 行为 |
|------|------|
| 单目录解析/入库失败 | 不写入成功台账、打日志、下轮自动重试（D19，§5.5）；无 retry 上限、无人工介入 |
| bench 字段缺失（如 vllm 无 e2el/input_len） | 落盘脚本填 `"N/A"`（或 `null`），入库原样存；对比表对应单元格显示 N/A，不阻塞 |
| 启动命令无法解析出某参数 | 按框架默认值回填（§5.4.1）；无默认值的（如 mem_fraction）记 `"auto"` 进 extra |
| LLM 端点超时/5xx | 对用户回 `error` 事件（"模型服务暂不可用，请稍后重试"）；后端重试 1 次 |
| LLM 产出非法 QuerySpec | schema 校验错误回填给 LLM 自修正，最多 2 次，仍失败则请用户换个说法 |
| 查询命中 0 条 | 不生成空报告；Agent 明确告知没有匹配数据，并（经 `list_dimension_values`）提示库里实际有什么可选 |
| 下载链接过期 | HTTP 410 + 前端友好提示 |
| 日志 | 两进程统一结构化日志（JSON lines）输出到 stdout，由 docker 收集；scanner 每轮扫描输出摘要（新增 N、成功 M、失败 K） |

---

## 12. 待定项

| # | 事项 | 责任方 | 影响范围 | 状态 |
|---|------|--------|----------|------|
| P1 | 提供现有 `to_csv.py` 脚本 | 产品负责人 | 脚本改造基线 | ✅ 已提供，已复制到仓库根目录待改造 |
| P2 | result.csv 建模方式 | 你 | §6 数据模型 | ✅ 已定：**方案 A**（一次测试=一个文档，输入长度/并发作 metric 维度） |
| P3 | 新增建议指标 | 你 | §5.2 CSV 列 | ✅ 已定：纳入 Completed / Total_Input_Tokens / Total_Output_Tokens / P95（不含 Duration_s） |
| P4 | framework_version 获取方式 | 你 | §5.3 metadata | ✅ 已定：**入参手动传** `--framework-version` |
| P5 | 提供 LLM 端点地址/模型名（部署时填 config，不阻塞开发） | 产品负责人 | 仅部署 | ⏳ 待部署 |
| P6 | Mongo 部署形态 | 你 | §6.2 写入逻辑、部署 | ✅ 已定：**单节点**、无事务、Scanner 单进程串行处理；靠 `_id` 幂等去重，本设计不依赖多文档事务。仅需提供连接串与 `autores` 库读写账号 |

---

## 13. 后续里程碑建议

1. **M0 落盘脚本**：改造 `to_csv.py`（`--framework`/`--nas-dir`/`--gpu-type`/`--launch-cmd` 等入参、双框架字段映射、启动参数提取、生成 timestamp 目录 + result.csv + metadata.json）——本轮即可动工；
2. **M1 数据管道**：Mongo 连接/索引初始化 + Scanner 主循环（含 ingest_log 已入库判定）+ 解析器（面向 M0 固定 schema），用真实历史目录回灌验证；
3. **M2 报告流水线**：QuerySpec（含 exclude）→ 查询 → 取最新 → 对齐 → Excel，先用硬编码 spec 测试产出正确性；
4. **M3 Agent 循环**：接 LLM、三工具、澄清/排除/取数策略，串通 `/api/chat` SSE；
5. **M4 前端与部署**：单文件 chatbot 页面、docker-compose、内网上线试运行。

M0 是其余一切的前置；M1 与 M2/M3 之间无依赖，可并行。
