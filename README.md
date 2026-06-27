> **Status: This is my origin story, not my current main line. The project that led me to AI Workflow Diagnostics. Current main line: DoneTrace.**

# Synthora — 多模型智能编排系统

> **让你变聪明的 AI 陪伴者，不是更快的搜索引擎。**

并行调用多个顶级大模型，由一个独立的评审模型对它们的回答做交叉验证与综合。核心机制是 **Quality Gate**（防止"综合反而稀释了最佳答案"）与**分歧可见化**（让你看到模型间在哪些点上一致、哪些点上分歧，从而判断答案的可信度）。

---

## 核心差异化

| 能力 | 说明 |
|------|------|
| **Quality Gate 三路径** | 综合最优 / 直接采用最佳单模型 / 标记低置信度——防止"综合稀释" |
| **分歧可见化** | 展示各模型在哪些观点上一致、哪些分歧，让你知道答案的确定性 |
| **自适应聚合** | 按题型选不同策略（事实题投票制、创意题取最佳、争议题多视角并存） |
| **Socratic 思维训练** | 暴露多模型分歧，引导独立推理，跨会话追踪认知进步 |
| **Companion Dispatcher** | 智能路由 + 后置引导，自动选择最适合的处理模式 |
| **Next-Step Guidance** | 基于回答内容和用户画像，推荐下一步探索方向 |

## 六种模式

| 模式 | 核心价值 | 延迟 | 模型策略 |
|------|---------|------|--------|
| **Light** | 快速 + 去幻觉 | <15s | 3 竞速取 1 |
| **Deep** | 深度推理 + 双重质疑 | 2-5 分钟 | 6 取 5 + Judge + 2 轮精炼 |
| **Research** | 全面调研 + 结构化报告 | 3-10 分钟 | 6 取 5 + MoA 两层 + 3 轮精炼 |
| **Socratic** | 分歧暴露 + 思维训练 | 多轮对话 | 3 取 2 + 分歧分析 + 引导生成 |
| **Roundtable** | 多模型圆桌讨论（实验中） | 多轮 | 可配置 |
| **Companion** | 智能路由，自动选择最佳模式 | 随路由结果 | — |

---

## 架构概览

Synthora 采用**六边形架构（Ports & Adapters）**，把领域逻辑与外部依赖（LLM 提供方、搜索引擎、数据库、Web 框架）彻底解耦。核心设计有五条主线：

- **六边形架构**：领域层只依赖抽象端口（port），具体实现（OpenAI / Claude / Gemini / Kimi / DeepSeek 等提供方、SQLite、Tavily 搜索）都作为适配器（adapter）插在外圈。换模型、换数据库、换搜索源不触碰核心逻辑。
- **管道模式（Pipeline）**：一次查询被拆成可组合的阶段——路由 → 并行取数 → 搜索增强 → 验证 → 聚合 → 精炼 → 质量门。每种模式（Light/Deep/Research/...）只是这些阶段的不同编排组合。
- **事件驱动（EventBus）**：阶段之间通过事件总线解耦，管道执行过程对外以事件流暴露，支撑 SSE 流式输出与异步的二层质量监控（Layer 2 抽样评估）。
- **自适应聚合（Adaptive Aggregation）**：聚合不是"无脑求平均"。系统先判断题型（事实 / 创意 / 争议 / 技术 / 推理 / 时效），再选择对应策略——事实题走投票收敛、创意题取最佳、争议题保留多视角并存——并由 Quality Gate 决定是综合、是直采单模型、还是标记为低置信。
- **统一 LLM 适配器**：所有提供方都被归一到 OpenAI 兼容格式之下，由统一适配器处理鉴权、可选代理、重试与按模型独立路由。新增一个模型通常只是 `config.yaml` 加一行 + 配一个 key。

```
                ┌──────────────────────────────────────────┐
   用户查询 ──▶  │  Router → 并行取数 → 搜索 → 验证 → 聚合      │
                │            │                    ▲            │
                │            ▼                    │            │
                │        EventBus  ◀───  Quality Gate / 精炼   │
                └──────────────────────────────────────────┘
                  外圈适配器：LLM 提供方 · 搜索 · SQLite · Web
```

技术上对应的目录大致是：`domain/`（领域逻辑与端口）、`services/`（编排、聚合、流式）、`adapters/`（提供方 / 存储 / 搜索实现）、`api/`（FastAPI 应用）、`cli/`（命令行入口）。

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 架构 | 六边形架构 + 管道模式 + 事件驱动 + 自适应聚合 |
| 后端 | Python 3.11+ / FastAPI / Uvicorn（异步） |
| 前端 | React 19 + Vite 6 + TypeScript + TailwindCSS + Framer Motion |
| AI 通信 | httpx（异步）/ OpenAI SDK 兼容格式（统一适配器） |
| 数据库 | SQLite（用户 / 会话 / Memory-lite） |
| 配置 | Pydantic + YAML + FeatureFlags |
| CLI | Click + Rich |

---

## 快速开始

### 1. 后端

**安装依赖**（任选其一）：

```bash
# 方式 A：uv（推荐，快）
uv sync

# 方式 B：pip + 锁定版本
pip install -r requirements.txt
```

**配置密钥**：

```bash
cp .env.example .env
# 编辑 .env，至少填入你打算启用的模型 key 和对应 base_url；
# 模型阵容与每个模型走哪条 key 在 config.yaml 中配置。
```

**启动 API 服务**（任选其一）：

```bash
# 方式 A：CLI（推荐）
agoracle serve --host 127.0.0.1 --port 8000 --reload

# 方式 B：直接用 uvicorn 拉起 ASGI 工厂
uvicorn agoracle.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

启动后接口文档在 `http://127.0.0.1:8000/docs`。

**命令行直接提问**（无需起服务）：

```bash
agoracle ask "你的问题" --mode deep
```

### 2. 前端

```bash
cd web
npm install
npm run dev      # 开发服务器（Vite）
npm run build    # 生产构建
```

前端默认连接本地后端，开发时确保后端已在 `127.0.0.1:8000` 运行。

---

## 许可证

本项目采用 Business Source License 1.1（BSL 1.1），详见 [LICENSE](./LICENSE)。
如需了解其它授权方式，请在本仓库提交 issue 联系。
