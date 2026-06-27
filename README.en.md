# Synthora — where my path to DoneTrace started

English copy kept for direct links. The primary homepage is [README.md](README.md). Chinese version: [README.zh.md](README.zh.md).

> **Synthora is not my current product.** It is the origin story and public
> evidence: an early, archived multi-model orchestration experiment that led me
> to the work I do now on AI Workflow Diagnostics. The current main line is
> **[DoneTrace][donetrace]**.

In one line: Synthora ran several LLMs in parallel, used an independent judge
model to cross-check and synthesize their answers, and made the disagreement
between models visible. It was a real working system, not a slide deck — I just
don't recommend it as a primary tool today.

Three things you probably want to know:

- **What it is** — a working multi-model orchestration system: six modes, a
  Quality Gate, and visible disagreement. Full design is in the archive below.
- **Why it's still here** — it records how I went from "multi-model
  orchestration" to "AI workflow diagnostics." Deleting it erases that path.
- **Where to go now** — if you came for what I build today, see **The current
  main line** below. You don't need to run Synthora yourself.

---

## The current main line (start here)

- **[DoneTrace][donetrace]** — my current main line, in AI Workflow Diagnostics.
  It targets where AI work actually breaks: **"done" with no evidence**,
  **review that just confirms itself**, **context lost along the way**, and
  **weak handoff**.
- **[Fusion Paradigm][fusion]** — a lightweight protocol where a second model
  reviews blind, without seeing the first model's conclusion, to fight
  models rubber-stamping each other.
- **Async AI Workflow Snapshot** — the service entry for an async, one-shot
  diagnostic snapshot of an AI workflow. **Public intake form pending** — not
  open yet.

## From Synthora to DoneTrace

I first set out to build a multi-model orchestration product on a simple bet:
more models, better synthesis, more trustworthy answers. Synthora delivered that
— parallel fan-out, an independent judge, a synthesized final answer, and the
disagreement laid out for you.

But the real lesson was bigger than orchestration. AI work tends to break not
because a single answer is weak, but because "done" carries no evidence, review
confirms itself, context leaks away, and handoff is sloppy. Those are
**workflow-level** problems, and no amount of orchestration fixes them. That
realization became DoneTrace. Synthora stays here as the first stop on that road.

---

## Archived project

> Below is how Synthora worked back then (internal codename `agoracle`, frozen at
> v2.8.8), kept **only as a historical archive and public evidence**. It is not
> the current main line. For active work, go to [DoneTrace][donetrace].

### What made it different

| Capability | What it did |
|------|------|
| **Quality Gate (3 paths)** | synthesize / take the best single model / flag low confidence — so synthesis never dilutes the best answer |
| **Visible disagreement** | shows where models agree and where they split, so you can judge confidence |
| **Adaptive aggregation** | strategy by question type: vote on facts, take-best on creative, keep multiple views on contested |
| **Socratic training** | exposes divergence to push independent reasoning, tracked across sessions |
| **Companion Dispatcher** | smart routing + post-answer guidance |
| **Next-Step Guidance** | suggests what to explore next from the answer and the user profile |

### Six modes

| Mode | Core value | Latency | Model strategy |
|------|---------|------|--------|
| **Light** | Fast + de-hallucinated | <15s | race 3, keep 1 |
| **Deep** | Deep reasoning + double critique | 2–5 min | 6→5 + Judge + 2 refine rounds |
| **Research** | Full research + structured report | 3–10 min | 6→5 + 2-layer MoA + 3 refine rounds |
| **Socratic** | Surface disagreement + train thinking | multi-turn | 3→2 + divergence analysis + guidance |
| **Roundtable** | Multi-model roundtable (experimental) | multi-turn | configurable |
| **Companion** | Smart routing, auto-picks the mode | varies | — |

### Architecture

Synthora used a **hexagonal (ports & adapters) architecture** to keep domain
logic decoupled from external dependencies (LLM providers, search, database, web
framework). The domain layer depends only on abstract ports; concrete adapters
(OpenAI / Claude / Gemini / Kimi / DeepSeek / Perplexity, SQLite, Tavily) plug in
on the outside. A query flows through a composable pipeline — route → parallel
fan-out → search → verify → aggregate → refine → quality gate — with stages
decoupled over an **EventBus** that powers SSE streaming and async layer-2
quality sampling. Aggregation is **adaptive**: it classifies the question type
first, then the Quality Gate decides whether to synthesize, take the best single
answer, or flag low confidence.

Roughly: `domain/` (logic), `ports/` (abstract ports), `services/`
(orchestration, aggregation, streaming), `adapters/` (provider / storage / search
/ session), `api/` (FastAPI), `cli/` (command line).

### Stack

| Layer | Tech |
|------|------|
| Architecture | Hexagonal + pipeline + event-driven + adaptive aggregation |
| Backend | Python 3.11+ / FastAPI / Uvicorn / uvloop (async) |
| Frontend | React 19 + Vite 6 + TypeScript + TailwindCSS + Framer Motion |
| Models | httpx async + OpenAI-compatible unified adapter (OpenAI / Claude / Gemini / Kimi / DeepSeek / Perplexity) |
| Search | Tavily + native web search on some models |
| Storage | SQLite / aiosqlite (users · sessions · memory-lite) |
| Config | YAML + env + feature flags |
| CLI | Click + Rich |

### Archived run notes

> These commands only reproduce the archived build; they are not the recommended
> way to use anything today. For the current main line, go to [DoneTrace][donetrace].

```bash
# Backend — install deps
uv sync                       # or: pip install -r requirements.txt

# Backend — configure keys
cp .env.example .env          # fill in the model keys/base_urls you enable; roster lives in config.yaml

# Backend — start the API (or use uvicorn directly)
agoracle serve --host 127.0.0.1 --port 8000 --reload

# Backend — ask straight from the CLI, no server needed
agoracle ask "your question" --mode deep

# Frontend
cd web && npm install && npm run dev
```

API docs are served at `http://127.0.0.1:8000/docs` once the backend is up.

### License

Business Source License 1.1 (BSL 1.1). See [LICENSE](./LICENSE). For other
licensing terms, open an issue in this repository.

[donetrace]: https://github.com/aaronyi97/donetrace
[fusion]: https://github.com/aaronyi97/fusion-paradigm
