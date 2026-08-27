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

系统由两条相互独立的链路组成，共享同一个 SQLite 单文件数据库（本地磁盘，零部署）：

1. **数据管道（自动、无人值守）**：定时扫描 NAS 上的测试结果目录，解析 `result.csv` + `metadata.json`，写入 SQLite。
2. **报告服务（按需触发）**：Web 前端 chatbot 接收自然语言对比需求 → LLM Agent 多轮理解与澄清 → 确定性流水线查库、对齐、生成 Excel → 前端提供下载链接。

```
                          ┌─────────────────────────────────────────────┐
  测试人员                 │              内网服务器 (Docker Compose)      │
     │ 落盘                │                                             │
     ▼                    │  ┌───────────┐   写入   ┌──────────────────┐ │
 ┌────────┐   目录挂载     │  │  Scanner  ├────────►│  SQLite 单文件    │ │
 │  NAS   │◄──────────────┼──┤ (进程 A)  │          │ /data/autores.db │ │
 └────────┘               │  └───────────┘          │ test_runs        │ │
                          │                         │ ingest_log       │ │
  产品/项目同事             │                         └────────┬─────────┘ │
     │ 浏览器              │  ┌──────────────────────────────┐ │ 查询      │
     ▼                    │  │   API Server (进程 B, FastAPI)│◄┘          │
 ┌──────────┐  HTTP/SSE   │  │  ┌──────────┐ ┌─────────┐ ┌───────┐     │ │
 │ Web 前端  │◄────────────┼──┤  │ 静态前端  │ │ Agent   │ │ 报告   │     │ │
 │ chatbot  │             │  │  │ (单文件)  │ │ 工具循环 │ │ 流水线 │     │ │
 └──────────┘             │  │  └──────────┘ └────┬────┘ └───┬───┘     │ │
                          │  └────────────────────┼──────────┼─────────┘ │
                          └───────────────────────┼──────────┼───────────┘
                                                  ▼          ▼
                                          OpenAI 兼容     临时报告目录
                                          LLM 端点        (TTL 清理)
```

---

## 2. 架构决策记录（ADR）

以下决策已逐项讨论确认，是本设计的边界条件：

| # | 决策点 | 结论 | 理由 / 备注 |
|---|--------|------|------------|
| D1 | 交互入口 | **自建 Web 前端 chatbot**（暂不接入办公 IM） | 办公软件暂无法接入；Excel 通过前端下载链接获取。接口层预留未来接 IM 的扩展位（见 §10.3） |
| D2 | NLU 方案 | **外部 OpenAI 兼容 LLM 端点**，地址/密钥由使用方在 config 文件中填写 | 端点支持 function calling；系统不绑定任何具体模型 |
| D3 | 数据库 | **SQLite**（单文件、零部署）：一次测试=一行，维度与结构化启动参数为独立列，metrics 以 JSON 列内嵌 | 见 §6；单节点、量小、Scanner 单写，SQLite 足够；替代早期 PostgreSQL / MongoDB 方案，省去外部数据库实例依赖 |
| D4 | 部署环境 | **内网 Linux 服务器 + NAS 目录挂载** | Scanner 以本地文件系统方式读 NAS 挂载点 |
| D5 | 后端技术栈 | **Python + FastAPI** | openpyxl 生成 Excel、标准库 sqlite3 访问数据库，零额外驱动依赖 |
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
| D18 | 参数入库形态 | **折中**：并行度（tp/dp/pp/ep/cp）+ 核心通用开关（kv_cache_dtype、hicache_enabled、flexkv_enabled、torch_compile、quantization、attention_backend）建 `test_runs` 独立列（可直接 WHERE/建索引）；框架专属细节（hicache_ratio 等）进 `extra` JSON 列 | 高频对比维度可直接筛选，避免表结构因框架特性膨胀，见 §6 |
| D19 | 半成品防护 | **取消完成标记机制**：csv/json 均由脚本硬编码原子生成，不存在半成品。入库解析失败即**不记录该 timestamp 目录**（下轮自动重试），成功才记入台账 | 简化 §5.5（原 §5.2 完成标记方案作废） |
| D20 | 取数策略 | **所有维度全同才取最新一次**；框架版本不同（如 vllm 0.5.11 vs 0.5.12）视为不同记录**全部取出**；不同框架（vllm vs sglang）的版本号**不可跨框架比较**，各自独立取 | 修订原 §7.2 的"latest"策略，见 §7.4 |
| D21 | 排除逻辑 | 取出数据可能过多，Agent 除"取哪些"外还支持"排除哪些"：QuerySpec 增加 `exclude` 字段，用户可要求剔除某维度值（如"去掉 A800 的"） | 见 §7.3、§7.4 |
| D23 | config.json 参与推导 | 落盘脚本与上传页面都接收模型 `config.json`（可选），与启动命令**一起**推导参数：`params` 列存实际生效值，`extra` 里留 `params_explicit` / `param_sources` / `param_notes` / `model_arch`。推导算法逐项照搬 vllm/sglang 上游，算不准的（sglang `mem_fraction_static` / `max_running_requests` / `attention_backend`）宁缺勿编 | `context_length`、`dtype`、`quantization`、`max-num-batched-tokens` 这类参数命令里通常不写，只解析命令则列全是 NULL，见 §5.4.2 |
| D24 | 模型元信息列拆分 | 口径不清的 `model_size` 列**彻底移除**，拆成 `model_params_b`（参数量，B）+ `model_weight_gb`（权重占用，GiB）。这两列与 `model_dtype` 三者**全部由 `config.json` 推导**，表单/CLI 只作可选覆盖：传了 config 就一个都不用填。仅 `model_params_b` 在推不出来时（没传 config 或 config 缺形状字段）要求手填——它是分组对比的主轴，不能为空。老库里的 `model_size` 留作死列，**不做换算回填** | 原列的三处文档口径互相矛盾（注释写参数量 GB、README 写权重占用 GB、前端占位符给 `72`），旧值到底是 GB 还是 B 判断不了；手填还容易把量化模型的 dtype 记成 `bf16`（那是激活精度）、把 MoE 的激活参数量当总参数量。既然 config 能算准，就别让人再填一遍，见 §5.4.3 |
| D25 | 显卡型号可 CRUD | 硬编码的 `GPU_MEMORY_GIB` dict **迁到** `tools/gpu_types.json` 作唯一真相；`/gpus` 页面与 MCP 五个固定指令（`gpu_type_list/get/create/update/delete`）做增删改查。写操作需 `confirm=true` 两段式；**内置 chat agent 不授予**这些工具。有库内引用的型号不能删；**不允许改名**（历史 `gpu_type` 是裸字符串） | 压测机 `tools/` 需能离线用，故不用 SQLite；型号表频繁增删（未发布卡）不该改代码。见 §5.4.4 |

---

## 3. 技术栈清单

| 层 | 选型 | 用途 |
|----|------|------|
| 语言 | Python ≥ 3.11 | 全部后端逻辑 |
| Web 框架 | FastAPI + uvicorn | REST API、SSE 流式回复、静态文件托管 |
| DB 访问 | 标准库 sqlite3（WAL 模式） | 零依赖；Scanner 单进程写、API 读，WAL 支持一写多读；进程内用锁串行化，API 侧查询在线程池中执行不阻塞事件循环 |
| CSV 解析 | pandas | result.csv 读取与清洗 |
| Excel 生成 | openpyxl | 对比报告渲染 |
| LLM 客户端 | openai 官方 SDK（指向自定义 base_url） | 任何 OpenAI 兼容端点均可用 |
| 调度 | Scanner 进程内 `while + sleep` 循环（简单可靠） | 定时扫描；不依赖系统 cron |
| 配置 | YAML（`config.yaml`）+ 环境变量覆盖 | 见 §9 |
| 前端 | 原生 HTML/CSS/JS 单文件 | chatbot 界面 |
| 部署 | Docker + docker-compose | scanner / api 两个容器，共享 data 卷承载 SQLite 文件与报告目录 |

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
    │   ├── client.py          # SQLite 连接（WAL）、全部 SQL 集中于此、建表建索引
    │   └── schema.py          # DDL、维度常量、表行 ↔ 文档 dict 互转（test_runs / ingest_log）
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
    │   ├── ingest/
    │   │   ├── upload.py      # 手工上传解析/校验/入库（§5.6）
    │   │   └── launch_params.py  # 复用 to_csv.py 的启动参数提取规则
    │   └── report/
    │       ├── query.py       # QuerySpec → SQL WHERE 构造与查询
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
| `--benchmark-kind {text,vlm}` | 否（默认 text） | 压测类型 → `metadata.benchmark_kind`；Scanner 路由到 `test_runs` / `vlm_test_runs` |
| `--framework {sglang,vllm}` | 是 | server 框架；决定启动命令参数提取规则 |
| `--bench-framework {sglang,vllm}` | 是 | 压测工具框架；决定按哪套字段名解析 bench 输出（见 §5.2） |
| `--bench-flush-cache {true,false}` | 是 | 压测前是否清缓存（入库对比维度） |
| `--input-dir` | 是 | bench 原始输出所在目录 |
| `--nas-dir` | 是 | NAS 挂载根路径（各测试人员挂载位置不同，由入参指定），脚本在其下创建时间戳目录 |
| `--gpu-type` | 是 | 显卡类型（如 `H20-141G`），写入 metadata.json |
| `--model` / `--model-version` | 是 | 模型名与版本 |
| `--model-params-b` | 否* | 参数量，单位 B（10⁹）；不传则由 `--model-config` 推导 → `model_params_b`。*没给 `--model-config`（或 config 缺形状字段）时必填 |
| `--model-weight-gb` | 否 | 权重实际占用，单位 GiB；不传则由 `--model-config` 推导 → `model_weight_gb` |
| `--model-dtype` | 否 | 权重精度（`bf16/fp16/fp8/int8/int4/fp4`）；不传则由 `--model-config` 推导 → `model_dtype` |
| `--launch-cmd` | 是* | **完整服务启动命令字符串**（colocated 必填；PD 分离改用 `--prefill-cmd`/`--decode-cmd`）；脚本据此提取结构化启动参数（见 §5.4） |
| `--model-config` | 否（强烈建议） | 模型目录下的 `config.json` 路径。**启动命令里通常不写**的参数（`context_length` / `dtype` / `quantization` / `max-num-batched-tokens` …）都是框架读它在运行时推导的；上面三个元信息列也靠它推导。不传则相应列留空（见 §5.4.1 / §5.4.3）。压测脚本自动落盘时无需单独配置——随机数据集压测必须给 bench 传 `--tokenizer`，而该路径就是模型目录，脚本里 `MODEL_CONFIG` 留空即取 `$TOKENIZER/config.json` |
| `--bench-cmd` | 否 | 完整 benchmark 命令字符串；作 `_autores_dims` 缺失时的兜底 |

> `prefix_rate` **不再**是 to_csv CLI 入参（已下沉为 text metrics 行键，由 `inject_dims.py` / CSV 列提供）。

脚本输出目录结构：

```
{nas_dir}/
└── 20260708_143000/          # 脚本按落盘时刻生成的时间戳目录（唯一标识）
    ├── result.csv            # 固定列头的量化指标表
    ├── metadata.json         # 结构化元信息 + 启动参数
    └── model_config.json     # 模型 config.json 原文（传了 --model-config 才有）
```

> `model_config.json` 存**原文**而非推导结果：推导规则会随 vllm/sglang 版本变，留下原文才能日后按新规则重算。网页上传流落盘同一套结构（`persist.write_run_dir`）。

### 5.2 result.csv：两框架字段映射

脚本把两框架 bench 输出的字段名统一映射为**同一套 metric 列**。以下映射基于对 vllm/sglang 最新 main 分支源码的核查：

| 统一 metric 列 | sglang JSON key | vllm JSON key | 备注 |
|----------------|-----------------|---------------|------|
| Input_Length | `random_input_len` / `_autores_dims.random_input_len` | 同左（优先 `_autores_dims`） | 压测脚本经 `inject_dims.py` 写入，解决 vllm JSON 无输入长度的老问题 |
| Concurrency | `max_concurrency` | `max_concurrency` | 一致 |
| Prefix_Rate | `_autores_dims.prefix_rate` | 同左 | **仅 text kind**；行键维度，缺列填 N/A |
| Image_Count / Video_Count / Image_Resolution | `_autores_dims.*` | 同左 | **仅 vlm kind**；行键维度；`Image_Resolution` 为 `HxW` 字符串 |
| Request_Throughput | `request_throughput` | `request_throughput` | 一致 |
| Input_Throughput | `input_throughput` | *(派生：`total_token_throughput - output_throughput`)* | vllm 无原生字段，落盘时计算 |
| Output_Throughput | `output_throughput` | `output_throughput` | 一致 |
| Total_Throughput | `total_throughput` | `total_token_throughput` | **名称不同** |
| TTFT_{Mean,Median,Std,P90,P95,P99}(ms) | `{mean,median,std,p90,p95,p99}_ttft_ms` | 同 | 一致；vllm P90/P95 需 `--metric-percentiles` |
| TPOT_{Mean,Median,Std,P90,P95,P99}(ms) | `{mean,median,std,p90,p95,p99}_tpot_ms` | 同 | 同上 |
| ITL_{Mean,Median,Std,P90,P95,P99}(ms) | `{mean,median,std,p90,p95,p99}_itl_ms` | 同 | 同上 |
| E2E_{Mean,Median,Std,P90,P95,P99}(ms) | `{mean,median,std,p90,p95,p99}_e2e_latency_ms` | `{mean,median,std,p90,p95,p99}_e2el_ms` | **名称不同**（vllm 是 `e2el`） |
| Completed | `completed` | `completed` | 成功请求数 |
| Failed | *(派生，见下)* | `failed_requests` | sglang：`errors` 非空计数 → `num_prompts-completed` → `len(output_lens)-completed` |
| Total_Input_Tokens / Total_Output_Tokens | `total_input_tokens` / `total_output_tokens` | 同 | 一致；**VLM 下是否含图像 token 两框架口径待实测，落地前不可跨框架比较** |
| KV_Cache_Hit_Rate(%) | `cache_report.cache_hit_rate_pct` | `kv_cache_hit_rate`（脚本注入） | 跨框架对齐 |
| SGLang_Spec_Accept_Length / vLLM_Spec_* | `accept_length` / — | — / `spec_decode_*` | 框架专属，不对齐 |

> **benchmark_kind**：`to_csv.py --benchmark-kind {text,vlm}` 写入 `metadata.benchmark_kind`，Scanner 按 kind 路由到 `test_runs` 或 `vlm_test_runs`。两套 kind **列结构相同**，差异在 metrics JSON 内的行键字段。
>
> **行键缺值约定**：只有 `Input_Length` / `Concurrency` 硬必填；其余行键（`Prefix_Rate` / `Image_*`）CSV 缺列则整列 N/A，不报错、不给默认值、上传表单不提供整份回退。`(1024,32,None)` 与 `(1024,32,0.0)` 是不同场景，报告里不会横向对齐。
>
> **prefix_rate 层级变更**：已从 run 级表列**下沉**为 text kind 的 metric 行键，不再出现在 `test_runs` 列 / 上传表单 / `compare_on`。

> 新指标写入 metrics JSON 列（schema-less），**无需改表结构**；老数据需重新 to_csv/上传才有新 key。

> **vllm 落盘注意**：① 加 `--percentile-metrics ttft,tpot,itl,e2el` 才有 E2E；② 加 `--metric-percentiles 90,95,99` 才有 P90/P95 扁平字段；③ 输入长度优先读 `_autores_dims`（压测脚本注入），否则回落 `--bench-cmd` 的 `--random-input-len`；④ `Input_Throughput` 由 total−output 派生。缺失字段一律填 `N/A`，不阻塞落盘。
>
> **sglang 落盘注意**：输出为 **JSONL**（每行一次 run）；需 `--output-details` 才写入 `errors`/`output_lens`，供派生 `Failed`（明细数组不入库）。

### 5.2.1 VLM 参数对齐与不可对齐项

统一语义用 `HxW` 分辨率字符串；脚本内转换：

| 统一语义 | sglang `bench_serving` | vllm `bench serve` |
|----------|------------------------|---------------------|
| 数据集 | `--dataset-name image` | `--dataset-name random-mm` |
| 每请求图片数 | `--image-count N` | `--random-mm-base-items-per-request N` + limit JSON |
| 分辨率 | `--image-resolution HxW` | `--random-mm-bucket-config '{"(H, W, 1)": 1.0}'` |
| 格式/内容 | `--image-format jpeg --image-content random` | **无等价开关**（不可跨框架对齐） |
| 图片数抖动 | 无 | `--random-mm-num-mm-items-range-ratio` → 脚本固定 `0` |
| 视频 | 无合成能力 | limit 里 `"video": 0`；`VIDEO_COUNT>0` 脚本直接报错 |

启动时对上述 flag 做 `--help` 探测，缺失则**硬失败**（不静默降级）。详见 `tools/vlm_benchs.sh`。

### 5.3 metadata.json：结构

```jsonc
{
  "benchmark_kind": "text",             // text → test_runs；vlm → vlm_test_runs
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

`launch_cmd` 原文始终保留，作为提取结果的溯源与人工复核依据。`prefix_rate` **不再**写入 metadata 顶层（已下沉为 text metrics 行键）。

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
> **本表已被 `tools/param_map.py` 取代**：那里按 flag 逐条记录别名、默认值种类（`STATIC` / `DERIVED` / `NA`）与语义类型，并标注哪些"看起来能比、实际不能比"。本表保留作背景说明，出现分歧时**以 param_map.py 为准**（它带上游 commit 基线，且有 `verify_param_map.py` 自动校验 flag 是否还存在）。

#### 5.4.2 config.json 驱动的参数推导（D23）

一批参数**启动命令里通常不写**，由框架在运行时读「模型 `config.json` + 硬件」推导。只解析命令拿不到它们，入库后相应列全是 NULL，既看不到实际生效值，也无法判断两次测试是否真的同配置。

实现在 `tools/model_config.py`（上游基线 commit 与 `param_map.py` 同步），逐项照搬上游算法而非另发明一套：

| 我们的字段 | sglang 推导来源 | vllm 推导来源 |
|-----------|----------------|--------------|
| `context_length` | `get_context_length`：候选键取**第一个命中** × rope factor | `derive_max_model_len_and_key`：候选键取**最小值**，`model_max_length` 无条件覆盖；yarn/longrope 另有分支 |
| `dtype` | `_get_and_verify_dtype`：`auto`+float32 → gemma\* 用 bfloat16，其余 float16 | `_resolve_auto_dtype`：`auto`+float32 → 平台首选（现代 CUDA = bfloat16） |
| `quantization` | `config.quantization_config.quant_method` | 同左 |
| `chunked_prefill_size` | `_handle_gpu_memory_settings` 显存分档 | `get_batch_defaults`：单卡 ≥70GiB 且卡名不含 a100 → 8192，否则 2048；再 `min(max_num_seqs × max_model_len, …)` |
| `max_running_requests` | *不推导*（见下） | `get_batch_defaults`：≥70GiB → 1024，否则 256；再 `min(…, chunked_prefill_size)` |
| `page_size` | 回填 1（`_page_size_default`） | 回填 16（`DEFAULT_BLOCK_SIZE`） |
| `prefix_caching` | 默认 True | `is_prefix_caching_supported`（encoder-decoder 系为 False） |
| `mem_fraction` | *不推导*（见下） | 0.92（静态字面量） |

> ⚠ **`context_length` 与 `dtype` 两边算法真的不同**，不能共用一套实现。例：Mistral 那种同时有 `max_position_embeddings=32768` 和 `model_max_length=16384` 的 config，sglang 先命中 `model_max_length`、vllm 取最小值后又被 `model_max_length` 覆盖，恰好同为 16384；但换成只有 `seq_length` 与 `max_position_embeddings` 两个键且数值不同的 config，两边结果就会分岔。

**刻意不推导的（宁缺勿编，列留 NULL）**：sglang 的 `mem_fraction_static`（公式依赖 attention backend 是否 MLA、DP attention 开关、moe_a2a_backend、cuda graph buffer 等运行时状态）、`max_running_requests`（由 KV pool 容量反推，而容量又取决于前者）、`attention_backend`（按 GPU 架构 × 模型架构 × kv dtype 分派）。

**params 的语义与来源留痕**：`params`（表列）存的是**实际生效值**——命令写了就用写的，没写就按上游逻辑推导。为了不丢"是不是用户显式设的"这一信息，`extra` 里同时留：

| `extra` 键 | 内容 |
|-----------|------|
| `params_explicit` | 只含命令里真正写了的参数（"用户意图"对比用） |
| `param_sources` | 每个参数的来源：`explicit` / `config` / `gpu` / `static` |
| `param_notes` | 推导说明与精度提示（如"命令里的 context_length 超过 config 支持值"） |
| `model_arch` | `config.json` 归一到我们字段后的模型结构：`num_layers` / `num_kv_heads` / `head_dim` / `sliding_window` / `is_moe` / `is_mla` / `kv_bytes_per_token` / vision 侧字段等 |
| `model_meta` | 元信息三列（§5.4.3）的**推导值**原始留档，便于日后与用户手填值对账 |

> `tp/pp/dp` 在算卡数时会先回填成 1，因此 `params_explicit` 必须在任何回填**之前**留档——光看"有没有值"分不出"命令写了 tp 1"和"命令没写 tp"。

**已知精度边界**：只吃 `config.json`。上游还会读 `tokenizer_config.json`（`model_max_length`）、`generation_config.json`、`hf_quant_config.json`，以及 config 里没写 dtype 时从 safetensors 头反查权重 dtype。这些不在上传范围内时相关推导会退化，`param_notes` 会显式写明。平台相关分支按 **NVIDIA CUDA + `vllm serve`（`UsageContext.OPENAI_API_SERVER`）** 取值——这是实际压测形态；昇腾/沐曦/平头哥的显存分档只是"容量凑巧落在哪个区间"的参考（同 `gpu_memory_presets.py`）。

#### 5.4.3 模型元信息三列（D24）

`model_params_b` / `model_weight_gb` / `model_dtype` 不是启动参数，而是 `test_runs` 的元信息列，原先靠上传表单手填。`config.json` 成为常规输入后改为推导，实现在 `tools/model_config.py` §2b / §4b。

> **口径变更**：原 `model_size` 列（注释说"参数量（GB）"、README 说"权重占用（GB）"、前端占位符给 `72`）三处口径不一致，用户实际填的大概率是参数量的 B 数。该列已**彻底移除**，拆成语义明确的两列。`migrate()` 只做 `ADD COLUMN`，老库里的 `model_size` 作为死列留着（既不读也不写），新建的库没有它。**不做换算回填**——旧值到底是 GB 还是 B 判断不了，编一个换算比留空更糟。

| 列 | 单位 | 谁说了算 | 推导方式 |
|----|------|---------|---------|
| `model_params_b` | B（10⁹） | config 推导，用户可覆盖；推不出来时才要求手填 | 按 config 形状字段逐块累加：embedding + 逐层（attn + MLP）+ MTP 层 + vision tower（含 patch embedding 与 merger） |
| `model_weight_gb` | GiB | config 推导，用户可覆盖 | 按段乘精度：层内线性层用量化精度，embedding / lm_head / vision tower 用 `torch_dtype` |
| `model_dtype` | — | config 推导，用户可覆盖 | **量化块优先**、计算 dtype 兜底 |

传了 config 的正常路径下这三个框全部留空，前端把推导值显示成 placeholder 而**不预填输入框**——预填等于让用户把推导值抄一遍再"确认"，之后就分不出哪些值是人填的。手填只是覆盖通道，留给"我知道这个 checkpoint 的真实布局和 config 声明的不一样"的情况。

**为什么 `model_dtype` 不能只读 `torch_dtype`**：量化 checkpoint 的 `torch_dtype` 是激活/计算精度（多为 `bfloat16`），不是权重精度。DeepSeek-V3 就是 `torch_dtype=bfloat16` + `quant_method=fp8`，只看前者会把它记成 `bf16`。`quant_method` → 精度的映射表取 vllm `QUANTIZATION_METHODS` 与 sglang `BASE_QUANTIZATION_METHODS` 的**并集**（只收会出现在 checkpoint 里的名字，不收 `--quantization` 才用的在线量化简写）；位宽写在别处的按方式分头读：`modelopt`/`quark` 看 `quant_algo`，`compressed-tensors` 看 `config_groups[].weights.{num_bits,type}`，`bitsandbytes` 看 `load_in_4bit/8bit`，`awq`/`gptq`/`moe_wna16` 看 `bits`。

**`model_params_b` 的估算精度**（实测见 `test/check_model_meta.py`）：稠密模型（Qwen2.5 / Llama / Mistral）、Mixtral 式 MoE（gpt-oss）、Qwen3-30B-A3B、以及两代 Qwen-VL（2 代 / 2.5 代）都**完全吻合官方参数量**；只有 DeepSeek-V3 差 0.3%（MTP 层的 `eh_proj` 按近似算）。norm / bias 等小张量一律不计（<0.1%）。核心形状字段（`num_hidden_layers` / `hidden_size` / `vocab_size`）缺任一项就返回 NULL 而不是给近似数。

MoE 层布局按 `first_k_dense_replace` + `decoder_sparse_step` 还原，`moe_layer_freq` 是逐层列表时还原不了，按"全是 MoE 层"处理并在 `param_notes` 里告警。

**vision tower 的两个坑**（都已处理，见 `_vision_dims` / `_vision_params`）：

1. **两代 Qwen-VL 的键名是反的**。2.5 代 `vision_config.hidden_size`=1280 是内部宽度、`out_hidden_size`=3584 是输出宽度；2 代反过来，`embed_dim`=1280 才是内部宽度、`hidden_size`=3584 是输出宽度。所以判据是"谁存在"而不是"谁优先"，按 `hidden_size` 优先读会让 2 代整个 vision tower 算大 8 倍。
2. **vision MLP 可能是 gated 的**。2.5 代是 `gate_proj`/`up_proj`/`down_proj` 三个矩阵（`hidden_act=silu`），2 代与 CLIP / SigLIP 是 `fc1`/`fc2` 两个。按激活函数名判断，否则 32 层累计差 140M。

patch embedding（Conv3d）与 Qwen-VL 系的 patch merger 都计入。CLIP / SigLIP 那类 projector 形状不在 `vision_config` 里，算不了就在 `param_notes` 里写明未计入（几十 M 量级）。

**合并规则**（`model_config.merge_model_meta`）：三列同一条规则——没填就用推导值，填了以填的为准并在不一致时告警。不静默覆盖手填值，是因为 config 只描述形状、不描述磁盘上真实存了什么（被裁剪的 checkpoint、混合量化、外挂 draft 权重都会让两者对不上）。比对方式按列分：`model_dtype` / `model_weight_gb` 直接比值，`model_params_b` 比相对偏差、超 **20%** 才报——阈值定得松是因为手填时习惯写标称值（"7B" 实际 7.62B，差 8%），要拦的是数量级错误：config 传错了模型，或把 MoE 的激活参数量（`Qwen3-30B-A3B` 的 `A3B`）当成总参数量填进来。

**刻意不推导**：`torchao` / `gguf` / `inc` / `modelslim` / `MIXED_PRECISION` 等逐层位宽不同的量化方式，config 里没有足够信息还原，`model_dtype` 与 `model_weight_gb` 均留 NULL 并在 `param_notes` 里写明原因。这三列都不参与任何计算、只做分组对比，编一个值会让"同模型两次上传对不上"，比留空更糟。

**参数量推不出来时的回退**：`model_params_b` 是唯一"必须有值"的一列。推不出来只有两种原因——没传 `config.json`，或 config 缺核心形状字段。两种情况都会在合并后校验时报错，要求显式指定（表单 `model_params_b` / CLI `--model-params-b`）。前端据 `inspect-config` 的返回决定是否强制该框必填：没选 config 或 config 没推出参数量时才拦。

#### 5.4.4 显卡型号注册表（D25）

型号表真相源是 **`tools/gpu_types.json`**（不是 SQLite、不是硬编码 dict）。上传白名单、按显存推导 `chunked_prefill_size`、报告层「N卡(M机)」换算都读它。选择 JSON 而非 DB：压测机侧 `tools/` 约定能脱离 `autores` 包单独跑，拷走目录（含该 JSON）即可离线校验 `--gpu-type`。

每条字段：

| 字段 | 说明 |
|------|------|
| `name` | 型号名（主键）；`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` |
| `memory_gib` | 单卡显存 GiB（1–2048） |
| `cards_per_machine` | 每机卡数（1–64）；取代旧的前缀猜测 |
| `vendor` | `nvidia` / `huawei` / `metax` / `cambricon` / `t-head` / `other` |
| `released` | 是否已发布 |
| `note` | 备注，≤200 字 |

读写入口：`tools/gpu_memory_presets.py`（mtime 缓存 + 原子写）。对外兼容 `gmp.GPU_MEMORY_GIB`（PEP 562 `__getattr__`，每次取最新 map）。业务校验与引用检查在 `autores/server/gpu_types.py`。

**管理入口**：

- 页面：`GET /gpus`（`frontend/gpus.html`）
- REST：`GET/POST /api/gpu-types`，`PATCH/DELETE /api/gpu-types/{name}`
- MCP 固定指令（**不**挂到内置 chat agent）：`gpu_type_list` / `gpu_type_get` / `gpu_type_create` / `gpu_type_update` / `gpu_type_delete`。写工具默认 `confirm=false` 只返回 preview，显式 `confirm=true` 才落盘。

**刻意约束**：

- 删除前查 `test_runs` + `vlm_test_runs` 引用数，`in_use > 0` 拒绝（历史 `gpu_type` 是裸字符串）
- `update` **不允许改 `name`**（改名等于让历史记录变孤儿；要改名只能新建 + 手工迁移）
- 路径可用 `AUTORES_GPU_TYPES_PATH` 覆盖（自检用临时文件）

### 5.5 Scanner：扫描、入库与半成品处理（D19）

- **扫描主循环**：按 `scanner.interval_seconds`（默认 300s）轮询 `scanner.benchmark_root`（NAS 挂载点）；列出一级子目录 → 过滤时间戳格式目录 → 与已成功台账比对 → 处理未入库目录。
- **无半成品概念**：csv/json 由脚本硬编码原子生成，不存在"写一半"。因此**取消完成标记文件、静默期等机制**。
- **失败即不记录**：解析/入库失败的目录**不写入成功台账**，仅打日志；下一轮扫描自然重试，直到成功才记账。无需 retry_count / abandoned 状态，无需人工干预流程。
- **幂等**：`test_runs.run_id`（= 目录名）为主键 + "只处理不在 ingest_log 台账中的目录"双重保证每目录仅入库一次；重复插入触发主键冲突（IntegrityError）被拒绝。
- **写入原子性**：单目录的整条 `test_runs` 记录（含 metrics JSON 列）一条 `INSERT` 写入，本身即原子；随后写 `ingest_log`。若中途失败，因主键幂等，下轮重试安全（详见 §6.2）。

### 5.6 手工上传入库（前端 `/upload` 子页面）

**场景**：数据分散在不同子系统与地区，未落在 Scanner 扫描的 NAS 目录下。测试人员在前端页面直接提交一份整理好的 CSV 与一个写有启动命令的 txt。

**输入**：

| 输入 | 必填 | 说明 |
|------|------|------|
| `csv_file` | 是 | 结果 CSV / xlsx，须含 `Input_Length` 与 `Concurrency` 两列（to_csv.py 的固定 schema）；其余列原样作为指标 |
| 启动命令文本 | 是 | 直接粘贴，原文保留；允许 `#` 注释行、空行与反斜杠续行，非空行拼接为一条命令。PD 分离改填 prefill / decode（+ 可选 router）三个框 |
| `config_file` | 否（强烈建议） | 模型目录下的 `config.json`。给了它才能推出 `context_length` / `dtype` / `quantization` / 批量调度默认值等命令里通常不写的参数（§5.4.2），以及元信息三列（§5.4.3）；不传照样能入库，相应列留空 |
| 表单字段 | 是 | `framework` / `framework_version` / `model` / `gpu_type` / `bench_framework` / `bench_flush_cache`——无法从上述文件推断，是 `metadata.json` 在上传流里的替代品 |
| 表单字段（可选覆盖） | 否 | `model_version` / `model_params_b` / `model_weight_gb` / `model_dtype`——留空即按 `config.json` 推导值入库；填了以填的为准，不一致时回显告警（§5.4.3）。未传 config 时 `model_params_b` 变必填 |

> 选好 `config.json` 后前端立刻调 `POST /api/upload/inspect-config` 回显识别到的架构 / 层数 / KV 头数 / 量化方式，以及推导出的参数量 / 权重占用 / 权重精度（写进对应输入框的 placeholder，不预填），当场发现传错文件（例如误传 `tokenizer_config.json`，或给多模态模型传了纯文本 config）。提交成功后的回显会给每个参数标出来源（`←config` / `←显存` / `←默认`，无标记即命令显式写的），并单列一行"模型元信息"显示三列的生效值。

**与目录流的一致性**（关键约束）：

- CSV 行→metric 记录复用 `scanner.parser` 的列名归一与数值转换（`N/A`→`None`、整数去小数点），两条路径解析同一份 CSV 结果逐字段相同；
- 启动参数提取**按文件路径加载 `tools/to_csv.py` 的 `extract_launch_params`**，而非复制规则——否则同一条命令走两条路径会得到不同 params，库里出现虚假差异；`framework` 决定用哪套规则与默认值回填（§5.4.1、§5.4.2）；
- 上传的 `config.json` 原文落盘为 `model_config.json`，目录结构与 to_csv.py 一致，崩溃后重扫可复原；
- **推导只发生在入库前**。Scanner 不重跑推导——同一个目录在不同 AutoRes 版本下必须解析出同一行数据，否则台账会随代码升级悄悄漂移。唯一例外是目录里有 config 原文、但 `metadata.extra` 没带 `model_arch`（老目录或手工拼的目录），此时补一份模型结构字段，只加字段、不动 params；
- 产出文档结构与 `parse_run_dir` 完全一致，走同一个 `db.insert_run`，因此上传的记录与扫描的记录在查询/对齐/报告环节无差别。

**run_id 与幂等**：上传无源目录，`run_id` 由服务器时间生成为 `upload_YYYYMMDD_HHMMSS`；同秒冲突时追加 `_1`、`_2`…（人工上传低频，冲突极少）。前缀 `upload_` 便于日后区分手工与自动记录，`extra.ingest_source = "manual_upload"` 同样标注来源。台账以 `run_id` 自身作为 `source_dir`，避免与扫描目录名冲突。

**校验与反馈**：所有非法输入（缺列、空文件、维度列为空、txt 无有效内容、框架非法、编码无法识别、超出体积上限）返回 **400 + 中文原因**，用户修正后可重试；入库成功后回显解析出的 params 与未识别参数（`extra.unrecognized`），供人工复核提取结果是否符合预期。

---

## 6. 数据模型（SQLite）

两张业务表 + 一张台账。`test_runs`（text kind）与 `vlm_test_runs`（vlm kind）**列结构完全相同**：每行 = 一次完整测试；指标以 JSON 列内嵌，行键字段也在 metrics 内。`ingest_log` 是**已入库目录台账**。

`schema.BENCH_KINDS` 注册表：

- `text` → 表 `test_runs`，行键 `input_length` / `concurrency` / `prefix_rate`
- `vlm` → 表 `vlm_test_runs`，行键 `input_length` / `concurrency` / `image_count` / `video_count` / `image_resolution`

`compare_on` / run 级 `filters` 只能取 run 级维度（`ALL_DIMENSIONS`，**不含**行键）；行键只能进 `metric_filters`。

数据库为单文件（`database.path`，默认 `/data/autores.db`），建库建表建索引全部由代码启动时自动完成（幂等），零人工初始化。**文件须放本地磁盘，不要放 NAS**——网络文件系统上 SQLite 锁不可靠。

### 6.1 `test_runs` / `vlm_test_runs` 表

两表 DDL 相同（`vlm_test_runs` 仅换表名），示意：

```sql
CREATE TABLE test_runs (
    run_id            TEXT PRIMARY KEY,   -- = timestamp 目录名（天然唯一，即 source_dir）
    run_timestamp     TEXT NOT NULL,      -- 由目录名解析，ISO 8601

    -- ── 元信息维度 ──
    model             TEXT NOT NULL,
    model_version     TEXT NOT NULL,
    model_params_b    REAL,               -- 参数量，单位 B（10^9）；7B 模型记 7.62
    model_weight_gb   REAL,               -- 权重实际占用，单位 GiB；7B bf16 约 14.2（§5.4.3）
    model_dtype       TEXT,               -- 权重精度 bf16|fp16|fp8|int8|int4|fp4
    framework         TEXT NOT NULL,      -- sglang | vllm
    framework_version TEXT NOT NULL,      -- 落盘入参手动传（P4 已定）
    gpu_type          TEXT NOT NULL,      -- 来自 --gpu-type
    launch_cmd        TEXT NOT NULL,      -- 原文，溯源

    -- ── 结构化启动参数：独立列（D18，高频对比维度可直接 WHERE）──
    tp INTEGER, dp INTEGER, pp INTEGER, ep INTEGER, cp INTEGER,
    kv_cache_dtype    TEXT,
    hicache_enabled   INTEGER,            -- 0/1，读出时还原 bool
    flexkv_enabled    INTEGER,
    torch_compile     INTEGER,
    quantization      TEXT,
    attention_backend TEXT,

    -- ── 框架专属细节 + 未识别参数 ──
    extra             TEXT NOT NULL DEFAULT '{}',   -- JSON

    -- ── 指标内嵌：JSON 数组；每个元素含行键 + 指标 ──
    -- text 行键: input_length, concurrency, prefix_rate
    -- vlm  行键: input_length, concurrency, image_count, video_count, image_resolution
    -- 形如 [{"input_length":1024,"concurrency":32,"prefix_rate":0.0,"Output_Throughput":3200.0,...}, ...]
    metrics           TEXT NOT NULL,      -- JSON

    created_at        TEXT NOT NULL
);
CREATE INDEX idx_test_runs_dims     ON test_runs (model, framework, framework_version, gpu_type);
CREATE INDEX idx_test_runs_parallel ON test_runs (tp, dp, pp, ep, cp);
-- vlm_test_runs 同结构、同索引命名模式
```

> **注意**：`prefix_rate` **不是**表列。同 run 级维度下多条压测按行键**并集合并** metrics（`merge_duplicates`），冲突取 `run_timestamp` 更新的；不同场景永不混算。
>
> **已废弃的 `model_size` 列**（D24）：口径不清，已拆成 `model_params_b` + `model_weight_gb`。`migrate()` 只做 `ADD COLUMN`（SQLite 删列要重建表，风险不值当），故老库里 `model_size` 作为死列留着、既不读也不写；新建的库没有它。

代码层保留"文档 dict"形态作为内部契约：`db/schema.py` 提供表行 ↔ dict 互转（params 子对象在读出时由参数列重组），下游对齐/工具/Agent 逻辑与存储引擎解耦——这正是本次从 MongoDB 平滑切换到 SQLite 只动 db 层的原因。

### 6.2 `ingest_log` 表（已入库/未入库追踪，D19）

这是"哪些目录已入库、哪些未入库"的核心机制：

```sql
CREATE TABLE ingest_log (
    source_dir  TEXT PRIMARY KEY,   -- = timestamp 目录名
    run_id      TEXT,
    ingested_at TEXT NOT NULL
);
```

**判定逻辑**（§5.5 Scanner 每轮执行）：

1. 列出 NAS 根目录下所有时间戳格式子目录，得到集合 `on_disk`。
2. 查 `ingest_log` 所有 `source_dir`，得到已入库集合 `ingested`。
3. **待处理 = `on_disk` − `ingested`**，逐个解析入库。
4. 单目录成功后，**先写 `test_runs` 行，再写 `ingest_log` 记录**。两步之间即便崩溃也安全——两表主键都是目录名，重跑时 `INSERT` 因主键冲突被拒，不会产生重复数据。
5. **失败即不写 ingest_log**（D19）——该目录下轮扫描自然重新出现在"待处理"集合里，自动重试。因此 `ingest_log` 里存在 = 已成功入库，不存在 = 未入库（含从未处理和处理失败两种，行为上都会被下轮重试）。

> **崩溃边界**：若"写完 test_runs、还没写 ingest_log"时崩溃，下轮该目录仍被视为未入库、重新处理；此时 `test_runs` 的 `INSERT` 因主键已存在报 `IntegrityError`，Scanner 捕获该异常视为"已入库"，补写 ingest_log 即可。（SQLite 本可把两条 INSERT 放同一事务一步到位，此处保留双主键幂等逻辑，与存储引擎无关、可移植。）

> 为什么单列 `ingest_log` 而不直接查 `test_runs` 是否有该主键？两者主键一致，理论上查 `test_runs` 也能判定。单列 `ingest_log` 的好处：① 台账极轻量，扫描比对快；② 语义清晰，未来若要记录"处理但主动跳过"（如空目录）可扩展字段而不污染数据表。

**几点说明**：

- **并发模型**：连接开 WAL 模式（一写多读），Scanner 容器写、API 容器读，两容器共享同一 data 卷（同宿主机本地盘）；进程内所有 DB 操作经锁串行化。单节点、量小，无需更重的并发方案。
- `gpu_type` 合并了原设计的 gpu_model + gpu_version（落盘只收单一 `--gpu-type` 字段）。
- 会话与报告不持久化（D13），因此**没有** conversation / report 表。

---

## 7. 报告服务设计（API Server，进程 B）

### 7.1 混合架构总览（D8）

```
用户消息 ──► [阶段一：LLM 工具循环]                [阶段二：确定性流水线]
             理解意图、拉取库内维度值、             QuerySpec ─► SQLite 查询
             模糊对齐、发现歧义则反问用户、    ──►   ─► 数据对齐（内嵌指标→宽表）
             最终产出一份结构化 QuerySpec           ─► openpyxl 渲染 Excel
                                                   ─► 返回下载链接
```

关键原则：**LLM 只产出"查什么"（QuerySpec），永远不接触"查到的数"**。指标数值从 SQLite 到 Excel 全程由代码搬运，杜绝 LLM 幻觉污染报告数据。

### 7.2 阶段一：LLM 工具循环

标准 function-calling 循环：把用户消息 + 会话历史发给 LLM，LLM 或者回复文本（澄清/闲聊），或者调用工具；工具结果回填后继续循环，直到 LLM 调用 `submit_query_spec`（触发 Excel 报告）、完成 `analyze_saturation` 后输出结论文本，或直接输出纯文本回复。单轮循环上限 `llm.max_tool_rounds`（默认 8）防失控。

**工具清单**（5 个）：

| 工具 | 入参 | 返回 | 用途 |
|------|------|------|------|
| `summarize_reports` | 可选 `filters` | 按显卡×模型的记录计数 | 盘点库内有多少报告 |
| `list_dimension_values` | `dimension`（取自 `schema.ALL_DIMENSIONS`），可选 `filters` | 库内该维度的去重值列表 + 各值的记录数 | 归一化对齐（D14）：用户说"4090"，LLM 拉取 gpu_type 实际值后自行对齐；也用于澄清时向用户列候选 |
| `count_matching_runs` | 一组维度等值/排除条件 | 命中的记录数量 + 若数量 ≤ 20 则附简要清单 | 提交前预检：0 条 → 告知无数据；数量过多 → 提示加约束或排除（D21） |
| `analyze_saturation` | `filters` / `exclude` / 可选 `run_id`、SLO、检测器阈值 | `{ok, runs, markdown, caveats}`；命中过多则 `ok:false` | 性能饱和点 / hardware wall：按 `input_length` 给出墙并发、推荐运行点、瓶颈与置信度；**不是**循环出口，模型整理结论文本后回复 |
| `submit_query_spec` | 完整 QuerySpec（见 7.3） | 校验结果；通过则触发阶段二 | Excel 对比报告任务的出口；后端对 spec 做严格 schema 校验，非法则把错误回给 LLM 修正 |

**System prompt 要点**（`prompts.py`，设计要求而非全文）：

- 角色：性能测试数据查询助手，任务是把对比需求转化为 QuerySpec，或调用饱和分析工具。
- 明确"对比轴（compare_on）"与"约束项（filters）"的概念，对应初稿 §6。
- 强制行为规则：
  1. 对齐任何用户提到的维度值之前，**必须**先调 `list_dimension_values` 确认库内真实值，禁止凭空猜测拼写；
  2. 出现歧义（一个口语值匹配多个库内值 / 用户漏说必要约束导致对比不成立）时，**必须**向用户反问并列出候选项，禁止擅自选择（D7）；
  3. 提交 Excel 报告前**必须**先 `count_matching_runs` 预检；
  4. **取数策略（D20）**：只有当一组记录的**所有维度完全相同**时才取最新一次；框架版本不同（如 vllm 0.5.11 与 0.5.12）视为不同记录，**全部取出**，不做去重；**不同框架**（vllm 与 sglang）的版本号**不可跨框架比较**，须各自独立呈现。向用户说明这一行为。
  5. **排除逻辑（D21）**：当结果过多、或用户明确要求"去掉某某"时，用 QuerySpec 的 `exclude` 字段剔除指定维度值，而非重新构造复杂 filter。
  6. **饱和分析**：用户问饱和点/性能墙/推荐并发时调用 `analyze_saturation`，禁止肉眼估墙；汇报须含前提、wall、推荐并发、瓶颈、置信度，并转述 `caveats`。
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

1. **查询**：QuerySpec（含 `benchmark_kind`）→ SQL `SELECT ... WHERE`，取出匹配表（`test_runs` / `vlm_test_runs`）的行。filters/compare_values 转为 `=`/`IN` 条件、exclude 转为 `NOT IN`（NULL 值不被误杀）；metric_filters（行键）在取出后于 Python 侧过滤内嵌指标数组。`compare_on` 必须是 run 级维度，不能是行键。
2. **合并去重**：结果按"run 级维度组合"分组，同组多条 run 的 metrics **按行键取并集**合并（`merge_duplicates`）；同一行键冲突时取 `run_timestamp` 更大的。**不同框架版本、不同框架各自成组，全部保留**——不会因版本不同被误合并。不同场景各占各的行，绝不混算。
3. **对齐**：把各文档的内嵌指标透视为矩阵宽表 —— **行 = 当前 kind 的全部行键组合，列 = 每个指标一个"列组"，组内再按对比轴的各个取值展开**；某组合缺某指标时该单元格填 `N/A`。报告附带 `coverage`（全列非 N/A 的对齐行数）。
4. **渲染**（纯数据对比表，D12）：单个 sheet；首行标题区注明约束项（如"模型: GLM-4.5 | 框架: sglang | TP: 8"），随后是对比表。表体版式：
   - **双层表头**：第 1 行为指标名（跨该指标列组合并），第 2 行为对比轴各取值；左侧按 kind 动态渲染行键列（text 3 列 / vlm 5 列），纵向合并两行。
   - **差异列**：当对比轴恰好为两个取值时，每个指标组末尾追加一列 `A vs B`，值为相对差异 `(A - B) / B`，按百分比格式显示；任一侧为 `N/A` 或分母为 0 时该单元格填 `N/A`。对比轴取值数 ≠ 2 时不生成差异列，并在标题区说明。
   - **块汇总**：每个 `Input_Length` 块结束后插入一行，填该块内各差异列的均值（红色加粗）；`N/A` 不参与均值计算。
   - 说明行标注对齐率与合并来源（`_merged_from`）。
   - 仅做基础可读性格式（表头加粗、冻结表头与维度列、列宽自适应），不加图表、不加结论。
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
| POST | `/api/upload` | 手工上传入库（multipart）：`csv_file` + `launch_file` + 5 个元信息表单字段；成功返回 run_id/指标行数/解析出的 params，校验失败返回 400 并说明原因 |
| GET | `/api/upload/options` | 上传表单可选项（框架列表，取自 §5.4.1 默认值表，前后端共用同一来源） |
| GET | `/` | 返回 `frontend/index.html` |
| GET | `/upload` | 返回 `frontend/upload.html`（手工上传子页面） |

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

database:                             # ← SQLite 单文件，零部署（D3）
  path: "/data/autores.db"            # 放本地磁盘（勿放 NAS）；文件不存在自动创建
  # 表名固定：test_runs、ingest_log（代码内常量，无需配置）

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

**两个服务**（数据库为 SQLite 单文件，无独立数据库容器，D3）：

| 服务 | 镜像 | 说明 |
|------|------|------|
| `scanner` | 项目镜像，入口 `python -m autores.scanner.main` | 挂载 NAS 目录（只读）+ config.yaml + 共享 `data` 卷（写 SQLite） |
| `api` | 同一项目镜像，入口 `uvicorn autores.server.main:app` | 暴露 8080；挂载 config.yaml + 共享 `data` 卷（读 SQLite + 写报告目录） |

scanner 与 api 用同一 Dockerfile 构建的同一镜像，仅入口命令不同（D9：同库两进程）。两容器共享同一个 `data` 命名卷（同宿主机本地盘），SQLite 开 WAL 模式支持 scanner 写 + api 读并存。建库建表建索引由进程启动时自动完成（幂等），零人工初始化，无任何外部数据库依赖。

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
| P6 | 数据库部署形态 | 你 | §6.2 写入逻辑、部署 | ✅ 已定：**SQLite 单文件**（先后经 PostgreSQL → 公司 Mongo → SQLite 收敛），Scanner 单进程串行处理，主键幂等去重；零外部依赖，无需任何数据库账号/实例 |

---

## 13. 后续里程碑建议

1. **M0 落盘脚本**：改造 `to_csv.py`（`--framework`/`--nas-dir`/`--gpu-type`/`--launch-cmd` 等入参、双框架字段映射、启动参数提取、生成 timestamp 目录 + result.csv + metadata.json）——本轮即可动工；
2. **M1 数据管道**：SQLite 建库建表 + Scanner 主循环（含 ingest_log 已入库判定）+ 解析器（面向 M0 固定 schema），用真实历史目录回灌验证；
3. **M2 报告流水线**：QuerySpec（含 exclude）→ 查询 → 取最新 → 对齐 → Excel，先用硬编码 spec 测试产出正确性；
4. **M3 Agent 循环**：接 LLM、三工具、澄清/排除/取数策略，串通 `/api/chat` SSE；
5. **M4 前端与部署**：单文件 chatbot 页面、docker-compose、内网上线试运行。

M0 是其余一切的前置；M1 与 M2/M3 之间无依赖，可并行。
