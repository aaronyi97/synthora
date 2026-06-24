"""
Quality Gate — decides whether to synthesize, adopt best single, or flag low confidence.

This is the defense against "synthesis dilution" — when Judge's synthesis
is worse than the best individual model response.

The gate runs on metadata from the parallel Extractor, NOT on Judge's self-assessment.
This keeps the evaluation independent from the synthesizer.
"""

from __future__ import annotations

from dataclasses import dataclass

from agoracle.domain.types import (
    MetadataExtraction,
    ModelEvaluation,
    ModelResponse,
    QualityGateResult,
)

# ── Default thresholds (used when no config passed) ──
DEFAULT_BEST_SINGLE_GAP = 0.3
DEFAULT_BEST_SINGLE_MIN_SCORE = 0.7
DEFAULT_LOW_CONFIDENCE_AVG = 0.4
DEFAULT_LOW_CONFIDENCE_META = 0.3
DEFAULT_DIVERGENCE_CONFIDENCE = 0.5
DEFAULT_ANSWER_CRITIC_CONFIDENCE = 0.8


@dataclass(frozen=True)
class QualityGateThresholds:
    """Tunable thresholds for the quality gate (typically from config.yaml)."""
    best_single_gap_threshold: float = DEFAULT_BEST_SINGLE_GAP
    best_single_min_score: float = DEFAULT_BEST_SINGLE_MIN_SCORE
    low_confidence_avg_threshold: float = DEFAULT_LOW_CONFIDENCE_AVG
    low_confidence_meta_threshold: float = DEFAULT_LOW_CONFIDENCE_META
    divergence_confidence_threshold: float = DEFAULT_DIVERGENCE_CONFIDENCE
    answer_critic_confidence_threshold: float = DEFAULT_ANSWER_CRITIC_CONFIDENCE


def evaluate_gate(
    responses: list[ModelResponse],
    metadata: MetadataExtraction,
    thresholds: QualityGateThresholds | None = None,
) -> QualityGateResult:
    """
    Determine the quality gate decision based on model evaluations.

    Three possible outcomes:
      SYNTHESIZED  — multiple models are complementary, synthesis adds value
      BEST_SINGLE  — one model clearly dominates, adopt it directly
      LOW_CONFIDENCE — all models uncertain or heavily divergent
    """
    t = thresholds or QualityGateThresholds()
    evals = metadata.model_evaluations

    if not evals:
        # No evaluation data — Extractor failed or returned empty.
        # v3.6: Always LOW_CONFIDENCE when we have no evaluation signal.
        # Blind synthesis without any quality signal risks diluting a good answer.
        return QualityGateResult.LOW_CONFIDENCE

    scores = compute_scores(evals, pairwise_mode=metadata.pairwise_evaluated)

    if not scores:
        # Scores computed but all zero — treat as no signal.
        return QualityGateResult.LOW_CONFIDENCE

    max_score = max(scores.values())
    avg_score = sum(scores.values()) / len(scores)
    sorted_scores = sorted(scores.values(), reverse=True)

    # ── Path 1a: Pairwise unanimous winner (v3.9) ────────────
    # In pairwise mode, scores are derived from win rates: {0, 0.25, 0.5, 0.75, 1.0}
    # for 5 models. A gap threshold applied to these discrete values is unreliable.
    # Instead: if the best model's pairwise score == 1.0 (won all comparisons),
    # it is an unambiguous unanimous winner — trigger BEST_SINGLE directly.
    # This is the only case where pairwise evidence is strong enough to skip synthesis.
    if metadata.pairwise_evaluated and max_score >= 1.0:
        return QualityGateResult.BEST_SINGLE

    # ── Path 1c: Pairwise strong-majority winner (v4.0) ──────
    # Path 1a only fires on unanimous winners (score=1.0). For cultural/factual/creative
    # question types, strategy sets a low best_single_gap_threshold (0.06–0.12) to prefer
    # best-single, but this threshold was ignored in pairwise mode.
    # Path 1c: if best pairwise score ≥ 0.75 (won ≥3/4 comparisons in 5-model setup)
    # AND gap to second-best ≥ best_single_gap_threshold, trigger BEST_SINGLE.
    # This makes strategy-level gap_override effective for pairwise evaluations.
    if metadata.pairwise_evaluated and len(sorted_scores) >= 2:
        second_best_pw = sorted_scores[1]
        gap_pw = max_score - second_best_pw
        if max_score >= 0.75 and gap_pw >= t.best_single_gap_threshold:
            return QualityGateResult.BEST_SINGLE

    # ── Path 1b: Best single model clearly dominates (non-pairwise) ──
    if not metadata.pairwise_evaluated and len(sorted_scores) >= 2:
        second_best = sorted_scores[1]
        gap = max_score - second_best
        if gap > t.best_single_gap_threshold and max_score > t.best_single_min_score:
            return QualityGateResult.BEST_SINGLE

    # ── Path 2: Low confidence — all models struggling ───────
    if avg_score < t.low_confidence_avg_threshold:
        return QualityGateResult.LOW_CONFIDENCE

    if metadata.confidence < t.low_confidence_meta_threshold:
        return QualityGateResult.LOW_CONFIDENCE

    # Heavy divergence with low confidence is also suspicious
    if metadata.has_divergence and metadata.confidence < t.divergence_confidence_threshold:
        return QualityGateResult.LOW_CONFIDENCE

    # ── Path 3: Normal synthesis ─────────────────────────────
    return QualityGateResult.SYNTHESIZED


def compute_score_gap(metadata: MetadataExtraction) -> float:
    """Compute the score gap between best and second-best model.

    Returns 0.0 if fewer than 2 models have scores.
    Used for observability — written into QueryResult for threshold tuning.
    """
    evals = metadata.model_evaluations
    if not evals:
        return 0.0
    scores = compute_scores(evals, pairwise_mode=metadata.pairwise_evaluated)
    if len(scores) < 2:
        return 0.0
    sorted_vals = sorted(scores.values(), reverse=True)
    return round(sorted_vals[0] - sorted_vals[1], 4)


def should_trigger_answer_critic(
    metadata: MetadataExtraction,
    thresholds: QualityGateThresholds | None = None,
) -> bool:
    """
    Determine if Answer Critic should run (Deep mode only).

    Conditional trigger (not always-on) based on review feedback:
    Only trigger when Judge's confidence is below threshold or divergence exists.
    """
    t = thresholds or QualityGateThresholds()
    if metadata.confidence < t.answer_critic_confidence_threshold:
        return True
    if metadata.has_divergence:
        return True
    return False


def get_best_response(
    responses: list[ModelResponse],
    metadata: MetadataExtraction,
) -> ModelResponse | None:
    """Get the best single model response (for BEST_SINGLE gate path)."""
    evals = metadata.model_evaluations
    if not evals:
        # Fallback: return first successful response (参考A: 不用长度等代理指标)
        successful = [r for r in responses if r.success and r.content]
        return successful[0] if successful else None

    scores = compute_scores(evals, pairwise_mode=metadata.pairwise_evaluated)
    if not scores:
        return None

    best_model_id = max(scores, key=scores.get)  # type: ignore
    for r in responses:
        if r.model_id == best_model_id and r.success:
            return r

    return None


def compute_scores(
    evals: dict[str, ModelEvaluation | dict],
    pairwise_mode: bool = False,
) -> dict[str, float]:
    """Compute composite scores from model evaluations.

    When pairwise_mode is True (scores derived from pairwise comparisons),
    uniqueness is not available, so weights are redistributed:
    accuracy=0.625, reasoning=0.375 (a model winning all comparisons
    can reach score=1.0 and correctly trigger BEST_SINGLE).
    """
    scores: dict[str, float] = {}
    for model_id, ev in evals.items():
        if isinstance(ev, ModelEvaluation):
            acc, reas, uniq = ev.accuracy, ev.reasoning, ev.uniqueness
        elif isinstance(ev, dict):
            acc = ev.get("accuracy", 0.0)
            reas = ev.get("reasoning", 0.0)
            uniq = ev.get("uniqueness", 0.0)
        else:
            continue

        if pairwise_mode:
            # v3.3: uniqueness now captured via winner_uniqueness in pairwise comparisons.
            # Old records without winner_uniqueness default to tie (uniq=0.5), neutral effect.
            # Weights aligned with non-pairwise mode: acc=0.5, reas=0.3, uniq=0.2.
            scores[model_id] = acc * 0.5 + reas * 0.3 + uniq * 0.2
        else:
            scores[model_id] = acc * 0.5 + reas * 0.3 + uniq * 0.2
    return scores
