# Synthora - where this all started

English-only edition. Chinese legacy notes are kept in [README.md](README.md).

> **Synthora is not the current main line.** It is the origin story: an early,
> archived multi-model orchestration experiment that led to my current work on
> AI Workflow Diagnostics. **The current main line is [DoneTrace][donetrace].**

Synthora ran several LLMs in parallel, used a judge model to cross-check and
synthesize their answers, and made model disagreement visible. I am keeping it
public as evidence of the path that led to DoneTrace, not as the project I
recommend people run today.

## Where this went next

If you landed here from my pinned GitHub repositories, start with the active work:

- **[DoneTrace][donetrace]** - the current main line for evidence, review, supervision, and handoff in AI workflows.
- **[Fusion Paradigm][fusion]** - a lightweight blind second-model review protocol.
- **Async AI Workflow Snapshot** - the service entry for async workflow diagnostics; public intake link pending.

## What Synthora was

- **Problem explored**: can multiple model answers become more reliable when disagreement is visible and a judge model reviews the synthesis?
- **Core mechanisms**: multi-model fan-out, Quality Gate, visible disagreement, adaptive aggregation, Socratic mode, Companion routing.
- **Stack**: Python / FastAPI backend, SQLite storage, React / Vite frontend, OpenAI-compatible model adapters.
- **Status**: archived origin project. The active direction is DoneTrace and AI Workflow Diagnostics.
- **License**: Business Source License 1.1. See [LICENSE](LICENSE).

## Why keep it public?

Synthora shows the starting point: I first tried to build a multi-model AI
orchestration product. The real lesson was larger than orchestration. AI work
breaks when "done" is not evidenced, review is self-confirming, context is lost,
and handoff is weak. That lesson became DoneTrace.

[donetrace]: https://github.com/aaronyi97/ai-collab-open-system
[fusion]: https://github.com/aaronyi97/fusion-paradigm
