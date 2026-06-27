# Synthora — 我走到 DoneTrace 的起点

中文主版。这个仓库是 Synthora 的历史说明，也是我后来走到 DoneTrace 的起点；英文镜像见 [README.en.md](README.en.md)。

> **Synthora 不是我现在的主产品。** 它是起点案例，也是公开证据：一个早期的、已经存档的多模型编排实验，把我一步步带到了今天做的 AI Workflow Diagnostics。当前主线是 **[DoneTrace][donetrace]**。

一句话讲清楚它：Synthora 把多个大模型并行跑起来，用一个独立的评审模型交叉验证、综合它们的回答，并且把模型之间的分歧明明白白摆出来。它能跑、也跑过，但我现在不建议你把它当主力工具。

读到这里，你大概想知道三件事：

- **它是什么** — 一个真能跑起来的多模型编排系统：六种模式、Quality Gate、分歧可见化。完整设计在下方「历史项目存档」。
- **为什么还留着** — 它记录了我从"多模型编排"走到"AI 工作流诊断"的真实路径。删了，这条路就看不见痕迹了。
- **你现在该去哪** — 冲着我现在做的东西来的，直接看下面「现在的主线」；Synthora 本身不必再跑一遍。

---

## 现在的主线（先看这里）

- **[DoneTrace][donetrace]** — 我现在的主线，领域是 AI Workflow Diagnostics（AI 工作流诊断）。它盯的是 AI 干活时最容易塌的那几块：**做完了却没有证据**、**复核在自说自话**、**上下文一路丢失**、**交接交不清楚**。
- **[Fusion Paradigm][fusion]** — 一个轻量协议：让第二个模型在看不到第一个结论的前提下盲审，专门对付"AI 互相附和"。
- **Async AI Workflow Snapshot** — 异步给一条 AI 工作流做一次诊断快照的服务入口。**公开表单待补**，暂未开放。

## 从 Synthora 到 DoneTrace

我一开始想做的是一个多模型编排产品，逻辑很朴素：模型多、综合得好，答案就更可信。Synthora 把这件事做出来了——并行取数、独立评审、综合定稿，再把分歧摆给你看。

但真正的教训比"编排"大得多。我后来发现，AI 干活出问题，往往不是单个答案不够好，而是：做完了没有证据、复核是自己确认自己、上下文一路丢、交接的时候交不清楚。这些都是**工作流层面**的毛病，再强的编排也补不上。

这个认识后来长成了 DoneTrace。Synthora 留在这儿，是这条路的第一站。

---

## 以下为历史项目存档

> 下面是 Synthora 当年（内部代号 `agoracle`，版本停在 v2.8.8）的设计与运行方式，**仅作历史存档和公开证据**。它不是当前主线，也不建议把它当成现在的入口来用。当前主线是 [DoneTrace][donetrace]。

### 当年的核心差异化

| 能力 | 说明 |
|------|------|
| **Quality Gate 三路径** | 综合最优 / 直接采用最佳单模型 / 标记低置信度——防止"综合反而稀释了最佳答案" |
| **分歧可见化** | 展示各模型在哪些观点上一致、哪些分歧，让你知道答案的确定性 |
| **自适应聚合** | 按题型选不同策略（事实题投票制、创意题取最佳、争议题多视角并存） |
| **Socratic 思维训练** | 暴露多模型分歧，引导独立推理，跨会话追踪认知进步 |
| **Companion Dispatcher** | 智能路由 + 后置引导，自动选择最适合的处理模式 |
| **Next-Step Guidance** | 基于回答内容和用户画像，推荐下一步探索方向 |

### 六种模式

| 模式 | 核心价值 | 延迟 | 模型策略 |
|------|---------|------|--------|
| **Light** | 快速 + 去幻觉 | <15s | 3 竞速取 1 |
| **Deep** | 深度推理 + 双重质疑 | 2–5 分钟 | 6 取 5 + Judge + 2 轮精炼 |
| **Research** | 全面调研 + 结构化报告 | 3–10 分钟 | 6 取 5 + MoA 两层 + 3 轮精炼 |
| **Socratic** | 分歧暴露 + 思维训练 | 多轮对话 | 3 取 2 + 分歧分析 + 引导生成 |
| **Roundtable** | 多模型圆桌讨论（实验中） | 多轮 | 可配置 |
| **Companion** | 智能路由，自动选择最佳模式 | 随路由结果 | — |

### 架构概览

Synthora 采用**六边形架构（Ports & Adapters）**，把领域逻辑和外部依赖（LLM 提供方、搜索引擎、数据库、Web 框架）彻底解耦。核心设计有五条主线：

- **六边形架构**：领域层只依赖抽象端口（port），具体实现（OpenAI / Claude / Gemini / Kimi / DeepSeek / Perplexity 等提供方、SQLite、Tavily 搜索）都作为适配器（adapter）插在外圈。换模型、换数据库、换搜索源，都不用动核心逻辑。
- **管道模式（Pipeline）**：一次查询被拆成可组合的阶段——路由 → 并行取数 → 搜索增强 → 验证 → 聚合 → 精炼 → 质量门。每种模式（Light / Deep / Research / …）只是这些阶段的不同编排组合。
- **事件驱动（EventBus）**：阶段之间通过事件总线解耦，管道执行过程以事件流对外暴露，支撑 SSE 流式输出和异步的二层质量监控（Layer 2 抽样评估）。
- **自适应聚合（Adaptive Aggregation）**：聚合不是无脑求平均。系统先判断题型（事实 / 创意 / 争议 / 技术 / 推理 / 时效），再选对应策略——事实题投票收敛、创意题取最佳、争议题保留多视角——最后由 Quality Gate 决定是综合、是直采单模型、还是标记为低置信。
- **统一 LLM 适配器**：所有提供方都被归一到 OpenAI 兼容格式之下，由统一适配器处理鉴权、可选代理、重试和按模型独立路由。新增一个模型，通常就是 `config.yaml` 加一行 + 配一个 key。

```
                ┌──────────────────────────────────────────┐
   用户查询 ──▶  │  Router → 并行取数 → 搜索 → 验证 → 聚合      │
                │            │                    ▲            │
                │            ▼                    │            │
                │        EventBus  ◀───  Quality Gate / 精炼   │
                └──────────────────────────────────────────┘
                  外圈适配器：LLM 提供方 · 搜索 · SQLite · Web
```

对应的目录大致是：`domain/`（领域逻辑）、`ports/`（抽象端口）、`services/`（编排 / 聚合 / 流式）、`adapters/`（提供方 / 存储 / 搜索 / 会话等实现）、`api/`（FastAPI 应用）、`cli/`（命令行入口）。

### 技术栈

| 层级 | 技术 |
|------|------|
| 架构 | 六边形架构 + 管道模式 + 事件驱动 + 自适应聚合 |
| 后端 | Python 3.11+ / FastAPI / Uvicorn / uvloop（异步） |
| 前端 | React 19 + Vite 6 + TypeScript + TailwindCSS + Framer Motion |
| 模型接入 | httpx 异步 + OpenAI 兼容格式统一适配（OpenAI / Claude / Gemini / Kimi / DeepSeek / Perplexity 等） |
| 搜索 | Tavily + 部分模型原生 web search |
| 数据库 | SQLite / aiosqlite（用户 · 会话 · Memory-lite） |
| 配置 | YAML + 环境变量 + Feature Flags |
| CLI | Click + Rich |

### 存档运行说明

> 以下命令仅用于复现当年的存档版本，不是现在推荐的使用方式。当前主线请走 [DoneTrace][donetrace]。

**后端 — 安装依赖**（任选其一）：

```bash
# 方式 A：uv（推荐，快）
uv sync

# 方式 B：pip + 锁定版本
pip install -r requirements.txt
```

**后端 — 配置密钥**：

```bash
cp .env.example .env
# 编辑 .env，至少填入你打算启用的模型 key 和对应 base_url；
# 模型阵容与每个模型走哪条 key 在 config.yaml 中配置。
```

**后端 — 启动 API 服务**（任选其一）：

```bash
# 方式 A：CLI（推荐）
agoracle serve --host 127.0.0.1 --port 8000 --reload

# 方式 B：直接用 uvicorn 拉起 ASGI 工厂
uvicorn agoracle.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

启动后接口文档在 `http://127.0.0.1:8000/docs`。

**后端 — 命令行直接提问**（无需起服务）：

```bash
agoracle ask "你的问题" --mode deep
```

**前端**：

```bash
cd web
npm install
npm run dev      # 开发服务器（Vite）
npm run build    # 生产构建
```

前端默认连接本地后端，开发时确保后端已在 `127.0.0.1:8000` 运行。

### 许可证

本项目采用 Business Source License 1.1（BSL 1.1），详见 [LICENSE](./LICENSE)。如需了解其它授权方式，请在本仓库提交 issue 联系。

[donetrace]: https://github.com/aaronyi97/ai-collab-open-system
[fusion]: https://github.com/aaronyi97/fusion-paradigm
