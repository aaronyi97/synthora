#!/usr/bin/env python3
"""
Blind evaluation: Compare Synthora aggregated answers vs single model answers
using external judges (GPT-5.2 + Claude Opus) without revealing which is which.

Usage:
    python scripts/blind_evaluation.py --questions 10 --mode deep
"""
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agoracle.config.loader import load_config
from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.adapters.judge.llm_judge import LLMJudge
from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
from agoracle.services.orchestrator import Orchestrator
from agoracle.services.prompt_loader import PromptLoader

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger("blind_eval")


@dataclass
class BlindAnswer:
    """Single answer for blind evaluation."""
    answer_id: str  # A/B/C/D (randomized)
    source: str     # "synthora" / "gpt52" / "opus" / "kimi"
    content: str
    latency_ms: int


@dataclass
class BlindQuestion:
    """Question with multiple answers for blind evaluation."""
    question_id: str
    question: str
    answers: List[BlindAnswer]


@dataclass
class JudgeRanking:
    """Judge's ranking of answers."""
    judge: str  # "gpt52" / "opus"
    rankings: List[str]  # answer_ids in order (best to worst)
    scores: Dict[str, int]  # answer_id -> score (1-100)
    reasoning: str


@dataclass
class BlindResult:
    """Result of blind evaluation for one question."""
    question_id: str
    question: str
    answer_sources: Dict[str, str]  # answer_id -> source
    judge_rankings: List[JudgeRanking]
    synthora_avg_rank: float
    synthora_win: bool


class BlindEvaluator:
    """Blind evaluation orchestrator."""
    
    def __init__(self, mode: str = "deep"):
        self.config = load_config()
        self.mode = mode
        PROJECT_ROOT = Path(__file__).resolve().parent.parent
        self.adapter = OpenAIModelAdapter(self.config)
        prompt_loader = PromptLoader(PROJECT_ROOT / "prompts")
        judge = LLMJudge(self.adapter, prompt_loader)
        extractor = LLMMetadataExtractor(self.adapter, prompt_loader)
        self.orchestrator = Orchestrator(
            config=self.config,
            model_adapter=self.adapter,
            judge=judge,
            extractor=extractor,
            prompt_loader=prompt_loader,
        )
        
        # Judge models
        self.judges = ["gpt52_thinking", "claude_opus_thinking"]
        
        # Single models to compare
        self.single_models = ["gpt52_thinking", "claude_opus_thinking", "kimi"]
    
    async def get_single_model_answer(self, model_id: str, question: str) -> tuple[str, int]:
        """Get answer from a single model."""
        import time
        start = time.time()
        
        try:
            from agoracle.domain.types import RoleCall, Role
            import uuid
            role_call = RoleCall(
                call_id=f"blind-{model_id}-{uuid.uuid4().hex[:6]}",
                model_id=model_id,
                role=Role.CONTRIBUTOR,
                system_prompt="你是一位专业的分析师，请对以下问题给出全面、准确、有深度的回答。",
                messages=[{"role": "user", "content": question}],
                timeout_seconds=120,
            )
            response = await self.adapter.call(role_call)
            latency_ms = int((time.time() - start) * 1000)
            return response.content, latency_ms
        except Exception as e:
            logger.error(f"Model {model_id} failed: {e}")
            return f"[Error: {str(e)}]", 0
    
    async def get_synthora_answer(self, question: str) -> tuple[str, int]:
        """Get Synthora aggregated answer."""
        import time
        start = time.time()
        
        try:
            from agoracle.domain.types import QueryContext, Mode
            context = QueryContext(
                question=question,
                mode=Mode(self.mode),
                resolved_mode=Mode(self.mode),
                web_search_enabled=False,
            )
            result = await self.orchestrator.execute(context)
            latency_ms = int((time.time() - start) * 1000)
            return result.final_answer, latency_ms
        except Exception as e:
            logger.error(f"Synthora failed: {e}")
            return f"[Error: {str(e)}]", 0
    
    async def collect_answers(self, question: str) -> BlindQuestion:
        """Collect answers from Synthora + single models."""
        logger.info(f"Collecting answers for: {question[:50]}...")
        
        # Get all answers in parallel
        tasks = [
            self.get_synthora_answer(question),
            *[self.get_single_model_answer(m, question) for m in self.single_models]
        ]
        results = await asyncio.gather(*tasks)
        
        # Build answer list
        sources = ["synthora"] + self.single_models
        answers = []
        for i, (content, latency) in enumerate(results):
            answers.append(BlindAnswer(
                answer_id=chr(65 + i),  # A, B, C, D
                source=sources[i],
                content=content,
                latency_ms=latency,
            ))
        
        # Shuffle answers to blind the evaluation
        import random
        random.shuffle(answers)
        
        return BlindQuestion(
            question_id=f"q{len(answers)}",
            question=question,
            answers=answers,
        )
    
    async def judge_answers(self, blind_q: BlindQuestion) -> List[JudgeRanking]:
        """Have judges rank the answers."""
        judge_rankings = []
        
        for judge_model in self.judges:
            logger.info(f"Judge {judge_model} evaluating...")
            
            # Build prompt
            answers_text = "\n\n".join([
                f"【答案 {a.answer_id}】\n{a.content}"
                for a in blind_q.answers
            ])
            
            prompt = f"""你是一位严格的答案质量评审专家。请对以下 {len(blind_q.answers)} 个答案进行盲评（你不知道它们来自哪个模型）。

问题：{blind_q.question}

{answers_text}

请按以下格式输出 JSON：
{{
  "rankings": ["A", "B", "C", "D"],  // 从最好到最差排序
  "scores": {{"A": 85, "B": 78, "C": 72, "D": 65}},  // 每个答案的总分（1-100）
  "reasoning": "简要说明排序理由（100字以内）"
}}

评分标准：准确性、完整性、多角度、清晰度、客观性。严格评分，使用完整刻度（20-90），强制区分。
"""
            
            try:
                from agoracle.domain.types import RoleCall, Role
                import uuid
                import re
                role_call = RoleCall(
                    call_id=f"blind-judge-{uuid.uuid4().hex[:6]}",
                    model_id=judge_model,
                    role=Role.JUDGE,
                    system_prompt="你是一位严格的答案质量评审专家。",
                    messages=[{"role": "user", "content": prompt}],
                    timeout_seconds=60,
                )
                response = await self.adapter.call(role_call)

                # Parse JSON response
                json_match = re.search(r'\{.*\}', response.content, re.DOTALL)
                if json_match:
                    result = json.loads(json_match.group())
                    judge_rankings.append(JudgeRanking(
                        judge=judge_model,
                        rankings=result["rankings"],
                        scores=result["scores"],
                        reasoning=result["reasoning"],
                    ))
                else:
                    logger.error(f"Judge {judge_model} returned invalid JSON")

            except Exception as e:
                logger.error(f"Judge {judge_model} failed: {e}")
        
        return judge_rankings
    
    def compute_result(self, blind_q: BlindQuestion, judge_rankings: List[JudgeRanking]) -> BlindResult:
        """Compute final result."""
        # Find Synthora's answer_id
        synthora_id = next(a.answer_id for a in blind_q.answers if a.source == "synthora")
        
        # Compute average rank
        ranks = []
        for jr in judge_rankings:
            try:
                rank = jr.rankings.index(synthora_id) + 1  # 1-indexed
                ranks.append(rank)
            except ValueError:
                pass
        
        avg_rank = sum(ranks) / len(ranks) if ranks else 999
        synthora_win = avg_rank == 1.0
        
        # Build source map
        answer_sources = {a.answer_id: a.source for a in blind_q.answers}
        
        return BlindResult(
            question_id=blind_q.question_id,
            question=blind_q.question,
            answer_sources=answer_sources,
            judge_rankings=judge_rankings,
            synthora_avg_rank=avg_rank,
            synthora_win=synthora_win,
        )
    
    async def evaluate_question(self, question: str) -> BlindResult:
        """Full evaluation pipeline for one question."""
        blind_q = await self.collect_answers(question)
        judge_rankings = await self.judge_answers(blind_q)
        result = self.compute_result(blind_q, judge_rankings)
        return result
    
    async def run(self, questions: List[str]) -> List[BlindResult]:
        """Run blind evaluation on multiple questions."""
        results = []
        for i, q in enumerate(questions, 1):
            logger.info(f"\n=== Question {i}/{len(questions)} ===")
            result = await self.evaluate_question(q)
            results.append(result)
            logger.info(f"Synthora rank: {result.synthora_avg_rank:.1f}, Win: {result.synthora_win}")
        
        return results


def save_results(results: List[BlindResult], output_path: Path):
    """Save results to JSON file."""
    data = {
        "timestamp": datetime.now().isoformat(),
        "total_questions": len(results),
        "synthora_wins": sum(1 for r in results if r.synthora_win),
        "synthora_avg_rank": sum(r.synthora_avg_rank for r in results) / len(results),
        "results": [asdict(r) for r in results],
    }
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"Results saved to {output_path}")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Blind evaluation")
    parser.add_argument("--questions", type=int, default=5, help="Number of questions")
    parser.add_argument("--mode", default="deep", choices=["light", "deep", "research"])
    parser.add_argument("--output", help="Output JSON file")
    args = parser.parse_args()
    
    # Sample questions
    test_questions = [
        "量子计算和经典计算的本质区别是什么？",
        "为什么民主制度在不同国家的实施效果差异很大？",
        "Rust 语言的所有权系统如何解决内存安全问题？",
        "儒家思想对现代东亚社会的影响有哪些？",
        "如何评价「电车难题」这个思想实验的价值？",
        "气候变化的主要驱动因素是什么？",
        "人工智能是否会取代大部分人类工作？",
        "如何理解「自由意志」这个哲学概念？",
        "区块链技术的核心创新是什么？",
        "为什么有些语言比其他语言更难学？",
    ]
    
    questions = test_questions[:args.questions]
    
    evaluator = BlindEvaluator(mode=args.mode)
    results = await evaluator.run(questions)
    
    # Save results
    if args.output:
        output_path = Path(args.output)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path(f"data/blind_eval/blind_{args.mode}_{timestamp}.json")
    
    save_results(results, output_path)
    
    # Print summary
    print(f"\n{'='*60}")
    print(f"Blind Evaluation Summary ({args.mode} mode)")
    print(f"{'='*60}")
    print(f"Total questions: {len(results)}")
    print(f"Synthora wins: {sum(1 for r in results if r.synthora_win)} ({sum(1 for r in results if r.synthora_win)/len(results)*100:.1f}%)")
    print(f"Synthora avg rank: {sum(r.synthora_avg_rank for r in results)/len(results):.2f}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
