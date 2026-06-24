"""
CLI entry point — ask questions, get answers.

Phase 1: Full pipeline with real model calls + streaming + feedback.
"""

from __future__ import annotations

import asyncio
import subprocess

import click
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from agoracle import __version__

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="agoracle")
def cli() -> None:
    """Agoracle — Multi-model AI orchestration system."""
    pass


@cli.command()
@click.argument("question")
@click.option(
    "--mode",
    type=click.Choice(["auto", "light", "deep", "research", "socratic"]),
    default="auto",
    help="Orchestration mode",
)
@click.option("--session", default=None, help="Session ID for multi-turn context")
@click.option(
    "--depth",
    type=click.IntRange(1, 3),
    default=None,
    help="Output depth: 1=answer only, 2=+divergence, 3=+individual responses",
)
@click.option("--stream/--no-stream", default=True, help="Enable streaming output")
def ask(question: str, mode: str, session: str | None, depth: int | None, stream: bool) -> None:
    """Ask a question to the orchestration system."""
    asyncio.run(_ask_async(question, mode, session, depth, stream))


async def _ask_async(
    question: str, mode: str, session: str | None, depth: int | None, stream: bool
) -> None:
    """Async implementation of the ask command."""
    from agoracle.adapters.judge.llm_judge import LLMJudge
    from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
    from agoracle.config.loader import PROJECT_ROOT, load_config
    from agoracle.domain.router import route
    from agoracle.domain.types import (
        Intent,
        Mode,
        OutputDepth,
        QueryContext,
        RouteDecision,
    )
    from agoracle.services.event_bus import EventBus
    from agoracle.services.prompt_loader import PromptLoader
    from agoracle.services.search_service import SearchService

    console.print(f"\n[bold cyan]Question:[/] {question}")
    console.print(f"[dim]Mode: {mode} | Stream: {stream}[/dim]\n")

    # === Load config ===
    config = load_config()
    console.print(
        f"[green]✓[/] Config loaded: {len(config.models)} models, "
        f"{len(config.modes)} modes"
    )

    # === Initialize components ===
    prompt_loader = PromptLoader(PROJECT_ROOT / "prompts")
    model_adapter = OpenAIModelAdapter(config)
    judge = LLMJudge(model_adapter, prompt_loader)
    extractor = LLMMetadataExtractor(model_adapter, prompt_loader)
    event_bus = EventBus()
    search_service = SearchService(
        api_key_env=config.search.api_key_env,
        max_results=config.search.max_results,
        search_depth=config.search.search_depth,
        include_answer=config.search.include_answer,
        timeout_seconds=config.search.timeout_seconds,
    ) if config.search.enabled else None

    available = model_adapter.available_models
    console.print(f"[green]✓[/] Models available: {available}")

    if not available:
        console.print(
            "[red]✗[/] No models available. Check API keys in .env file."
        )
        return

    # === Route ===
    if mode == "socratic":
        await _execute_socratic(question, config, model_adapter, judge, extractor, prompt_loader, event_bus, search_service)
        return

    import uuid as _uuid
    query_id = _uuid.uuid4().hex[:12]

    if mode == "auto":
        decision = route(question, query_id=query_id)
    else:
        resolved = Mode(mode)
        mode_cfg = config.modes.get(mode)

        output_depth = OutputDepth.LEVEL_1
        if depth:
            output_depth = [OutputDepth.LEVEL_1, OutputDepth.LEVEL_2, OutputDepth.LEVEL_3][depth - 1]
        elif mode == "deep":
            output_depth = OutputDepth.LEVEL_2
        elif mode == "research":
            output_depth = OutputDepth.LEVEL_3

        decision = RouteDecision(
            mode=resolved,
            web_search_enabled=True,
            critique_enabled=mode_cfg.critique_always_on if mode_cfg else False,
            intent=Intent.ANSWER,
            output_depth=output_depth,
        )

    resolved_mode = decision.mode
    if resolved_mode == Mode.AUTO:
        resolved_mode = Mode.LIGHT

    console.print(
        f"[green]✓[/] Router: mode={resolved_mode.value}, "
        f"web_search={decision.web_search_enabled}, "
        f"critique={decision.critique_enabled}, "
        f"depth={decision.output_depth.value}"
    )

    mode_config = config.modes.get(resolved_mode.value)
    if not mode_config:
        console.print(f"[red]✗[/] Mode '{resolved_mode.value}' not found in config")
        return

    console.print(f"[green]✓[/] Contributors: {mode_config.contributors}")
    judge_note = " [dim](also contributor)[/]" if mode_config.judge in mode_config.contributors else ""
    console.print(f"[green]✓[/] Judge: {mode_config.judge}{judge_note} | Extractor: {mode_config.extractor}")

    # === Build QueryContext ===
    context = QueryContext(
        query_id=query_id,
        question=question,
        mode=Mode(mode) if mode != "auto" else Mode.AUTO,
        resolved_mode=resolved_mode,
        intent=decision.intent,
        web_search_enabled=decision.web_search_enabled,
        critique_enabled=decision.critique_enabled,
        output_depth=decision.output_depth,
        question_type=decision.question_type,
    )

    # === Execute pipeline ===
    if stream:
        await _execute_streaming(context, config, model_adapter, judge, extractor, prompt_loader, event_bus, resolved_mode, search_service)
    else:
        await _execute_batch(context, config, model_adapter, judge, extractor, prompt_loader, event_bus, resolved_mode, search_service)

    # Play notification sound when pipeline finishes
    try:
        subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Glass.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


async def _execute_streaming(context, config, model_adapter, judge, extractor, prompt_loader, event_bus, resolved_mode, search_service=None):
    """Execute with streaming output — shows progress as it happens."""
    from agoracle.services.streaming import (
        ContributorDone,
        JudgeToken,
        PipelineComplete,
        PipelineError,
        PreviewAnswer,
        StageCompleted,
        StageStarted,
        execute_streaming,
    )

    console.print("\n[bold yellow]⏳ Pipeline running (streaming)...[/]\n")

    judge_buffer: list[str] = []
    preview_shown = False
    result = None

    async for event in execute_streaming(
        context=context,
        config=config,
        model_adapter=model_adapter,
        judge=judge,
        extractor=extractor,
        prompt_loader=prompt_loader,
        event_bus=event_bus,
        search_service=search_service,
    ):
        if isinstance(event, StageStarted):
            console.print(f"  [dim]▶ {event.stage}: {event.detail}[/]")

        elif isinstance(event, ContributorDone):
            icon = "✓" if event.success else "✗"
            color = "green" if event.success else "red"
            console.print(
                f"    [{color}]{icon}[/{color}] {event.model_id} "
                f"({event.latency_ms}ms)"
            )

        elif isinstance(event, PreviewAnswer):
            # Show fastest contributor's answer as preview while waiting for Judge
            console.print(Panel(
                Markdown(event.content),
                title=f"[bold yellow]Preview[/] ({event.model_id} — waiting for Judge synthesis...)",
                border_style="yellow",
                subtitle="[dim]Will be replaced by Judge's synthesized answer[/]",
            ))
            preview_shown = True

        elif isinstance(event, StageCompleted):
            console.print(f"  [dim]✔ {event.stage}: {event.detail}[/]")

        elif isinstance(event, JudgeToken):
            # First Judge token: indicate we're replacing the preview
            judge_buffer.append(event.token)
            if len(judge_buffer) == 1:
                if preview_shown:
                    console.print("\n[bold green]─── Judge synthesized answer ───[/]\n")
                else:
                    console.print()
            print(event.token, end="", flush=True)

        elif isinstance(event, PipelineComplete):
            result = event.result
            if judge_buffer:
                print()  # newline after streaming

        elif isinstance(event, PipelineError):
            console.print(f"\n[red]Error: {event.error}[/]")
            return

    if result:
        _display_result(result, resolved_mode, streamed=bool(judge_buffer))


async def _execute_batch(context, config, model_adapter, judge, extractor, prompt_loader, event_bus, resolved_mode, search_service=None):
    """Execute without streaming — shows result at the end."""
    from agoracle.services.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        config=config,
        model_adapter=model_adapter,
        judge=judge,
        extractor=extractor,
        prompt_loader=prompt_loader,
        event_bus=event_bus,
        search_service=search_service,
    )

    console.print("\n[bold yellow]⏳ Executing pipeline...[/]\n")
    result = await orchestrator.execute(context)
    _display_result(result, resolved_mode, streamed=False)


def _display_result(result, resolved_mode, streamed: bool = False):
    """Display the final query result."""
    if result.final_answer and not result.final_answer.startswith("系统错误"):
        if not streamed:
            console.print(Panel(
                Markdown(result.final_answer),
                title=f"[bold green]Answer[/] ({resolved_mode.value} mode)",
                border_style="green",
            ))

        cost_cny = result.estimated_cost_usd * 1.35
        cost_str = f" | Cost: ¥{cost_cny:.2f}" if result.estimated_cost_usd > 0 else ""
        console.print(f"\n[dim]Latency: {result.latency_ms}ms | "
                      f"Contributors: {result.contributor_count} | "
                      f"Confidence: {result.confidence:.2f} | "
                      f"Gate: {result.quality_gate_result}{cost_str}[/]")

        if result.has_divergence and result.divergence_summary:
            console.print(f"[dim yellow]Divergence: {result.divergence_summary}[/]")

        if result.key_insights:
            console.print(f"[dim]Insights: {', '.join(result.key_insights[:3])}[/]")

        # Feedback prompt
        console.print(
            f"\n[dim]Query ID: {result.query_id} | "
            f"Rate: agoracle feedback {result.query_id} --rating useful/inaccurate[/]"
        )
    else:
        console.print(Panel(
            result.final_answer or "No response",
            title="[bold red]Error[/]",
            border_style="red",
        ))


async def _execute_socratic(question, config, model_adapter, judge, extractor, prompt_loader, event_bus, search_service=None):
    """Execute Socratic mode — interactive multi-turn dialogue."""
    from agoracle.adapters.profile.json_profile import JsonProfileStore
    from agoracle.config.loader import PROJECT_ROOT
    from agoracle.services.socratic_orchestrator import SocraticOrchestrator

    profile_store = JsonProfileStore(PROJECT_ROOT / config.memory.profile_path)

    orch = SocraticOrchestrator(
        config=config,
        model_adapter=model_adapter,
        judge=judge,
        extractor=extractor,
        prompt_loader=prompt_loader,
        event_bus=event_bus,
        search_service=search_service,
        profile_store=profile_store,
    )

    console.print("\n[bold cyan]🧠 Socratic Mode[/]")
    console.print("[dim]Phase 1: 多模型分析中，请稍候...[/]\n")

    session = await orch.start_session(question)

    # Show divergence summary
    if session.divergence_map:
        dm = session.divergence_map
        console.print(f"[green]✓[/] 分析完成: {len(dm.divergence_points)} 个分歧点, "
                      f"{len(dm.consensus_points)} 个共识点 "
                      f"({session.phase1_latency_ms}ms)")
        if dm.consensus_points:
            console.print(f"[dim]共识: {', '.join(dm.consensus_points[:3])}[/]")
        console.print()

    # Interactive dialogue loop
    console.print("[bold yellow]Phase 2: 苏格拉底对话[/]")
    console.print("[dim]输入你的想法。输入 /reveal 查看完整答案，/quit 退出。[/]\n")

    if session.turns:
        console.print(Panel(
            session.turns[-1].content,
            title="[bold cyan]思维教练[/]",
            border_style="cyan",
        ))

    while not session.revealed and session.guide_rounds_used < session.max_guide_rounds:
        try:
            user_input = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]对话结束[/]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("/reveal", "揭示答案", "/r"):
            break
        if user_input.lower() in ("/quit", "/q", "退出"):
            console.print("[dim]对话结束[/]")
            await orch.finish(session)
            return

        guide_turn = await orch.respond(session, user_input)
        console.print(Panel(
            guide_turn.content,
            title=f"[bold cyan]思维教练[/] [dim](轮次 {session.guide_rounds_used}/{session.max_guide_rounds}, {guide_turn.latency_ms}ms)[/]",
            border_style="cyan",
        ))

    # Reveal
    console.print("\n[bold green]═══ 揭示完整答案 ═══[/]\n")
    reveal_data = await orch.reveal(session)
    console.print(Panel(
        Markdown(reveal_data["full_answer"]),
        title="[bold green]专家综合答案[/]",
        border_style="green",
    ))

    # Show divergence map
    if reveal_data["divergence_map"] and reveal_data["divergence_map"].divergence_points:
        console.print("\n[bold yellow]分歧图谱[/]")
        for dp in reveal_data["divergence_map"].divergence_points:
            console.print(f"  [bold]• {dp.topic}[/] (共识度: {dp.consensus_ratio:.0%}, 难度: {dp.difficulty})")
            for pos in dp.positions:
                models = ", ".join(pos.get("models", []))
                console.print(f"    {pos.get('stance', '?')}: {pos.get('summary', '')} [dim]({models})[/]")

    # Evaluate cognitive patterns
    console.print("\n[dim]正在分析你的思维模式...[/]")
    session = await orch.finish(session)
    if session.cognitive_snapshot:
        cs = session.cognitive_snapshot
        console.print(f"\n[bold]认知快照[/]")
        console.print(f"  推理深度: {cs.reasoning_depth:.0%}")
        console.print(f"  细腻度: {cs.nuance_recognition:.0%}")
        if cs.anchoring_detected:
            console.print(f"  [yellow]⚠ 检测到锚定偏差[/]")
        if cs.confirmation_bias:
            console.print(f"  [yellow]⚠ 检测到确认偏差[/]")
        if cs.blind_spots:
            console.print(f"  盲点: {', '.join(cs.blind_spots)}")

    console.print(f"\n[dim]对话轮次: {session.guide_rounds_used} | "
                  f"Phase 1: {session.phase1_latency_ms}ms[/]")


@cli.command()
def models() -> None:
    """Show available models and their status."""
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
    from agoracle.config.loader import load_config

    config = load_config()
    adapter = OpenAIModelAdapter(config)

    table = Table(title="Model Status")
    table.add_column("ID", style="bold")
    table.add_column("Name")
    table.add_column("Model")
    table.add_column("Status")

    for model_id, mc in config.models.items():
        available = adapter.supports_model(model_id)
        status = "[green]Ready[/]" if available else "[red]No Key[/]"
        table.add_row(model_id, mc.name, mc.model_name, status)

    console.print()
    console.print(table)
    console.print()


@cli.command()
@click.argument("query_id")
@click.option("--rating", type=click.Choice(["useful", "inaccurate", "too_shallow", "too_slow"]),
              required=True, help="Your rating")
@click.option("--comment", default=None, help="Optional comment")
def feedback(query_id: str, rating: str, comment: str | None) -> None:
    """Rate a previous answer."""
    asyncio.run(_feedback_async(query_id, rating, comment))


async def _feedback_async(query_id: str, rating: str, comment: str | None) -> None:
    from agoracle.adapters.feedback.json_feedback import JsonFeedbackStore
    from agoracle.config.loader import PROJECT_ROOT

    store = JsonFeedbackStore(PROJECT_ROOT / "data" / "feedback.jsonl")
    await store.record(query_id, rating, comment)
    console.print(f"[green]✓[/] Feedback recorded: {query_id} → {rating}")


@cli.command()
def stats() -> None:
    """Show feedback statistics."""
    asyncio.run(_stats_async())


async def _stats_async() -> None:
    from agoracle.adapters.feedback.json_feedback import JsonFeedbackStore
    from agoracle.config.loader import PROJECT_ROOT

    store = JsonFeedbackStore(PROJECT_ROOT / "data" / "feedback.jsonl")
    s = await store.get_stats()
    if not s:
        console.print("[dim]No feedback recorded yet.[/]")
        return

    table = Table(title="Feedback Statistics")
    table.add_column("Rating", style="bold")
    table.add_column("Count")

    for rating, count in sorted(s.items()):
        table.add_row(rating, str(count))

    console.print()
    console.print(table)
    console.print()


@cli.command()
@click.option("--host", default="127.0.0.1", help="Server host")
@click.option("--port", default=8000, help="Server port")
@click.option("--reload", is_flag=True, help="Enable auto-reload for development")
def serve(host: str, port: int, reload: bool) -> None:
    """Start the API server."""
    import uvicorn
    from agoracle.api.app import create_app

    console.print(f"\n[bold green]🚀 Agoracle API Server[/]")
    console.print(f"[dim]Listening on http://{host}:{port}[/]")
    console.print(f"[dim]Docs: http://{host}:{port}/docs[/]\n")

    if reload:
        uvicorn.run(
            "agoracle.api.app:create_app",
            host=host, port=port, reload=True, factory=True,
            timeout_keep_alive=600,
        )
    else:
        app = create_app()
        uvicorn.run(app, host=host, port=port, timeout_keep_alive=600)


if __name__ == "__main__":
    cli()
