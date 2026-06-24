"""
Router — determines mode, intent, output depth, web search, and critique.

Phase 0: Rule-based engine (keywords + heuristics).
Phase 1: Add Intent awareness + OutputDepth binding.
Phase 2: Hybrid (rules + model + system memory feedback).
Phase 3: Socratic mode routing (explicit only).

v2.0 changes:
  - Intent decision (ANSWER / GROWTH) — Phase 1: always ANSWER for auto routing
  - OutputDepth binding per mode (Light=L1, Deep=L2, Research=L3)
  - Socratic signal words reserved (not used in auto routing)
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path

from agoracle.config.schema import AppConfig, ModelConfig
from agoracle.domain.types import Intent, Mode, OutputDepth, QuestionType, RouteDecision, Turn

# v3.3: LLM zero-shot router — lazy import to avoid circular deps at module load
# Populated on first call to _llm_classify_question_type()
_llm_router_client: "AsyncOpenAI | None" = None
_llm_router_model: str = ""
_llm_router_base_url: str = ""
_llm_router_api_key: str = ""

logger = logging.getLogger(__name__)

# v2.3: JSONL log for routing decisions — training data for future model-based router
_ROUTING_LOG_PATH = Path(os.getenv(
    "ROUTING_LOG_PATH",
    "data/logs/routing_decisions.jsonl",
))


# ============================================================
# Signal word lists
# ============================================================

_RESEARCH_SIGNALS = [
    r"调研", r"对比", r"分析.*优缺点", r"综述", r"研究",
    r"survey", r"compare", r"pros\s+and\s+cons", r"overview",
    r"全面分析", r"深度报告", r"行业", r"市场.*分析",
    r"技术选型", r"方案对比", r"趋势",
]

_DEEP_SIGNALS = [
    r"为什么", r"怎么.*设计", r"架构", r"原理", r"本质",
    r"分析.*代码", r"debug", r"性能.*优化", r"策略",
    r"根本原因", r"底层.*逻辑", r"深入.*解释",
    r"why", r"how.*design", r"architect", r"root\s*cause",
    r"trade.?off", r"权衡", r"设计.*方案",
]

_OFFLINE_SIGNALS = [
    # Technical/coding
    r"代码", r"算法", r"数学", r"证明", r"翻译这",
    r"写一个", r"实现", r"正则", r"排序", r"code",
    r"function", r"class\s", r"def\s", r"sql",
    r"逻辑题",
    # v3.0: Analytical/Reasoning/Meta-cognition — these rarely need web search,
    # enabling Kimi thinking mode for deeper reasoning
    r"思维.*框架", r"方法论", r"认知.*偏差", r"思维.*模型",
    r"第一性原理", r"批判性思维", r"元认知",
    r"逻辑.*推理", r"三段论", r"逆否命题", r"充分.*必要",
    r"概率", r"统计.*分析", r"贝叶斯",
    r"设计.*模式", r"架构.*设计", r"系统.*设计",
    r"优缺点.*分析", r"利弊.*分析", r"SWOT",
    r"哲学", r"伦理.*分析", r"道德.*推理",
    r"假设.*检验", r"因果.*推断", r"反事实",
]

_REALTIME_SIGNALS = [
    r"最新", r"今天", r"202[4-9]", r"价格", r"天气",
    r"新闻", r"发布", r"版本", r"latest", r"current",
    r"stock", r"update", r"release", r"刚刚",
    # v3.0: 3份审计报告一致发现覆盖不足，补充常见实时需求信号
    r"近期", r"目前", r"现在", r"上个月", r"去年",
    r"汇率", r"比分", r"排名", r"股价", r"开盘", r"收盘",
    r"票房", r"政策", r"航班", r"什么时候",
    r"trend", r"news", r"price", r"now",
]

_FALSE_PREMISE_SIGNALS = [
    r"众所周知.*但", r"既然.*已经", r"为什么.*不能.*明明",
]

_ASSUMPTION_SIGNALS = [
    r"是不是", r"能不能", r"是否可以",
    r"假设", r"如果.*那么", r"一定",
    r"所有.*都", r"没有.*能",
]

# v2.0: Socratic mode signal words — RESERVED for Phase 3.
# Currently NOT used in auto routing. Socratic is explicit-only (--mode socratic).
_SOCRATIC_SIGNALS = [
    r"我.*想.*想", r"帮我.*思考", r"引导.*我",
    r"苏格拉底", r"socratic", r"不要.*直接.*答案",
    r"我.*自己.*判断", r"启发.*我",
]

# ============================================================
# Question type classification (Phase 2: smart aggregation)
# ============================================================

_FACTUAL_SIGNALS = [
    r"是什么", r"什么是", r"定义", r"谁是", r"哪个", r"多少",
    r"什么时候", r"在哪里", r"首都", r"人口", r"面积",
    r"what\s+is", r"who\s+is", r"when\s+did", r"where\s+is",
    r"how\s+many", r"how\s+much", r"define",
    r"是否允许", r"能不能", r"有没有",
    r"谁发明", r"谁创造", r"哪年", r"哪个国家",
    r"距离.*多少", r"温度.*多少", r"速度.*多少",
    # v3.2: 科学事实类问题（常被误分为 TECHNICAL）
    r"能.*实现.*吗", r"可以.*实现.*吗", r"真的.*吗", r"是真的吗",
    r"有多少.*基因", r"有多少.*个", r"共有多少",
    r"量子", r"物理.*定律", r"科学.*事实",
]

_ANALYTICAL_SIGNALS = [
    r"为什么", r"原因", r"分析", r"评估", r"论证",
    r"影响", r"后果", r"意义", r"启示", r"关系",
    r"why", r"analyze", r"evaluate", r"impact", r"implications",
    r"因果", r"推理", r"论点", r"论据",
    r"权衡", r"trade.?off",
    r"为什么.*发生", r"为什么.*首先",
    r"根本原因", r"深层.*原因",
    r"政策.*导向", r"改革.*领域",
]

_TECHNICAL_SIGNALS = [
    r"代码", r"实现", r"算法", r"架构", r"设计.*模式",
    r"API", r"数据库", r"性能.*优化", r"debug", r"bug",
    r"code", r"implement", r"algorithm", r"architect",
    r"function", r"class\s", r"def\s", r"sql", r"regex",
    r"编译", r"部署", r"容器", r"微服务", r"分布式",
    r"redis", r"mysql", r"docker", r"kubernetes", r"nginx",
    r"(?:tcp|http|grpc|websocket)", r"缓存", r"索引", r"并发",
    r"LRU", r"hash.*map", r"线程.*池",
]

_CONTROVERSIAL_SIGNALS = [
    r"应该", r"应不应该", r"好还是", r"支持还是",
    r"争议", r"看法", r"观点", r"怎么看",
    r"should", r"opinion", r"controversial", r"debate",
    r"伦理", r"道德", r"公平", r"正义",
    r"会.*取代", r"取代.*人", r"威胁", r"利弊",
    r"好.*还是.*坏", r"赞成", r"反对",
    r"是否.*应该", r"值不值", r"有没有必要",
    r"有效.*方案吗", r"可行.*吗", r"合理吗",
    r"支持.*反对", r"反对.*支持",
    r"合法化", r"禁止.*还是", r"允许.*还是",
    r"更.*高效吗", r"更.*好吗", r"有害吗", r"危险吗",
    r"对.*有害", r"对.*有益",
]

_CREATIVE_SIGNALS = [
    r"写一.*(?:故事|诗|文章|段落|小说)", r"创作", r"想象",
    r"write.*(?:story|poem|essay)", r"creative", r"brainstorm",
    r"起个名", r"取名", r"口号", r"slogan",
]

_CULTURAL_SIGNALS = [
    r"文化", r"传统", r"习俗", r"民族", r"历史.*意义",
    r"中医", r"西医", r"传统医学",
    r"东方.*西方", r"中国.*启示", r"对.*启示",
    r"culture", r"tradition", r"civilization",
    r"儒家", r"佛教", r"哲学.*思想",
    r"科学性.*争议",
    r"异同", r"比较.*文化", r"制度.*差异",
    r"精神", r"信仰", r"宗教",
    r"民间", r"非.*物质.*文化遗产",
    r"文明", r"古代.*现代",
    # v3.2: 节日/风俗/历史人物/武士道等常见文化题
    r"节日", r"春节", r"中秋", r"端午", r"元宵", r"清明",
    r"鞭炮", r"风俗", r"礼仪", r"仪式",
    r"武士", r"武士道", r"骑士", r"武士精神",
    r"历史.*影响", r"对.*社会.*影响", r"对.*现代.*影响",
    r"日本.*文化", r"中国.*文化", r"韩国.*文化",
    r"古代.*文化", r"传统.*文化", r"民族.*文化",
]

_REASONING_SIGNALS = [
    r"数学", r"计算", r"证明", r"概率", r"统计",
    r"逻辑.*推理", r"三段论", r"逆否命题",
    r"如果.*那么.*可以.*得出", r"推导", r"得出.*结论",
    r"多少.*人", r"百分比", r"比例",
    r"math", r"proof", r"probability", r"calculate",
    r"排列", r"组合", r"方程",
    r"几何", r"代数", r"微积分", r"矩阵",
    r"充分.*必要", r"充要条件",
]

_META_COGNITION_SIGNALS = [
    r"知识管理", r"思维.*框架", r"方法论",
    r"批判性思维", r"创造性思维", r"元认知",
    r"信息过载", r"如何.*建立.*体系",
    r"如何.*平衡", r"如何.*决策",
    r"可操作.*框架", r"系统性.*思考",
    r"metacognition", r"methodology",
    r"认知.*偏差", r"思维.*模型", r"学习.*方法",
    r"学习法", r"思考方式", r"决策.*框架",
    r"确认偏误", r"幸存者偏差", r"锡定效应",
    r"如何避免", r"如何克服",
]

_WRITING_SIGNALS = [
    r"写一.*(?:邮件|信|文章|报告|简历|自我介绍|开场白|结语|总结|段落|介绍|说明|通知|公告)",
    r"帮我写", r"帮我起草", r"帮我润色", r"帮我改写", r"帮我翻译",
    r"润色.*文章", r"改写.*段落", r"优化.*文案", r"文案.*优化",
    r"write.*(?:email|letter|article|report|resume|bio|intro|summary|paragraph)",
    r"draft.*(?:email|letter|proposal|report)",
    r"rewrite", r"paraphrase", r"polish.*text", r"improve.*writing",
    r"翻译.*(?:成|为|到)", r"translate.*(?:to|into)",
]

_CODING_SIGNALS = [
    r"写.*(?:函数|方法|类|脚本|程序|代码)",
    r"实现.*(?:功能|接口|算法|模块|组件)",
    r"(?:debug|调试).*(?:代码|程序|脚本)",
    r"代码.*(?:报错|错误|bug|问题)",
    r"(?:python|javascript|typescript|java|go|rust|c\+\+|c#|swift|kotlin).*(?:代码|实现|写)",
    r"write.*(?:function|class|script|code|program)",
    r"implement.*(?:feature|interface|algorithm|module)",
    r"fix.*(?:bug|error|code)", r"code.*(?:review|refactor)",
    r"sql.*(?:查询|语句|写)", r"write.*sql",
    r"正则.*表达式", r"regex.*pattern",
]

_MATH_SIGNALS = [
    r"计算.*(?:结果|答案|值)", r"求.*(?:解|值|面积|体积|概率|期望)",
    r"解.*(?:方程|不等式|微积分|积分|导数)",
    r"证明.*(?:定理|命题|公式)",
    r"(?:微积分|线性代数|概率论|统计学|数论|组合数学).*(?:题|问题|计算)",
    r"\d+.*(?:\+|-|\*|/|\^|mod).*\d+",   # arithmetic expressions
    r"calculate", r"compute", r"solve.*(?:equation|integral|derivative)",
    r"prove.*theorem", r"math.*problem",
    r"排列组合.*(?:计算|求)", r"概率.*(?:是多少|计算|求)",
]


# v3.3: All valid QuestionType values for LLM zero-shot classification
_ALL_QUESTION_TYPE_VALUES = ", ".join(
    qt.value for qt in QuestionType if qt != QuestionType.UNKNOWN
)

# v3.3: LLM zero-shot classification prompt (v2: confidence-aware JSON output)
_LLM_CLASSIFY_SYSTEM = (
    "你是一个问题分类器。将用户问题分类为以下类别之一，输出 JSON，格式：{\"type\": \"类别名\", \"confidence\": 0.0-1.0}\n"
    f"{_ALL_QUESTION_TYPE_VALUES}\n\n"
    "分类规则：\n"
    "- writing: 需要写作、起草、润色、翻译文本的任务\n"
    "- coding: 写代码、调试代码、实现功能\n"
    "- math: 纯数学计算、解方程、证明定理\n"
    "- factual: 有唯一正确答案的事实查询\n"
    "- technical: 技术架构、系统设计、技术原理分析\n"
    "- analytical: 多维度分析、因果推理、评估论证\n"
    "- controversial: 价值判断、争议性观点、无唯一正确答案\n"
    "- cultural: 文化、历史、传统、跨文化比较\n"
    "- reasoning: 逻辑推理、形式推导（非数学计算）\n"
    "- meta_cognition: 思维方法、学习方法、认知框架\n"
    "- creative: 创意写作、头脑风暴、命名、口号\n"
    "confidence 含义：1.0=完全确定，0.5=模糊边界题。只输出 JSON，不要其他文字。"
)

# v3.3: Minimum confidence threshold for LLM result to override signal-word result.
# Below this threshold, signal-word result is kept (conservative fallback).
_LLM_CLASSIFY_MIN_CONFIDENCE = 0.70

_LEGACY_LLM_ROUTER_MODEL = "gemini-3-flash-preview"
_LLM_ROUTER_MODEL_ID = "gemini_3_flash"


def _resolve_llm_router_model_config(
    config: AppConfig | None = None,
    model_config: ModelConfig | None = None,
) -> ModelConfig | None:
    """Resolve router classifier model config, loading YAML lazily if needed."""
    if model_config is not None:
        return model_config

    if config is not None:
        resolved = config.models.get(_LLM_ROUTER_MODEL_ID)
        if resolved is None:
            logger.warning(
                f"[llm_router] model config {_LLM_ROUTER_MODEL_ID!r} not found in provided config; "
                f"falling back to legacy model {_LEGACY_LLM_ROUTER_MODEL!r}"
            )
        return resolved

    try:
        from agoracle.config.loader import load_config

        loaded_config = load_config()
        resolved = loaded_config.models.get(_LLM_ROUTER_MODEL_ID)
        if resolved is None:
            logger.warning(
                f"[llm_router] model config {_LLM_ROUTER_MODEL_ID!r} not found in config.yaml; "
                f"falling back to legacy model {_LEGACY_LLM_ROUTER_MODEL!r}"
            )
        return resolved
    except Exception as e:
        logger.warning(
            f"[llm_router] failed to load config.yaml for {_LLM_ROUTER_MODEL_ID!r}; "
            f"falling back to legacy model {_LEGACY_LLM_ROUTER_MODEL!r}: {e}"
        )
        return None


def _resolve_llm_router_runtime(
    config: AppConfig | None = None,
    model_config: ModelConfig | None = None,
) -> tuple[str, str, str]:
    """Return (model_name, base_url, api_key) for the LLM router."""
    resolved = _resolve_llm_router_model_config(config=config, model_config=model_config)
    if resolved is None:
        return (
            _LEGACY_LLM_ROUTER_MODEL,
            os.environ.get("GEMINI_FLASH_BASE_URL", ""),
            os.environ.get("GEMINI_FLASH_API_KEY", ""),
        )

    model_name = resolved.model_name or _LEGACY_LLM_ROUTER_MODEL
    base_url = os.environ.get(resolved.base_url_env, "") if resolved.base_url_env else ""
    key_envs = resolved.api_key_env_list or ([resolved.api_key_env] if resolved.api_key_env else [])
    api_key = next((os.environ.get(env_name, "") for env_name in key_envs if os.environ.get(env_name, "")), "")
    return model_name, base_url, api_key


async def _llm_classify_question_type_async(
    question: str,
    config: AppConfig | None = None,
    model_config: ModelConfig | None = None,
) -> tuple[QuestionType, float] | None:
    """Call gemini_3_flash for zero-shot question type classification.

    Returns (QuestionType, confidence) or None on any error.
    Confidence is 0.0-1.0; caller uses _LLM_CLASSIFY_MIN_CONFIDENCE to decide
    whether to override the signal-word result.
    Runs in parallel with fan-out (non-blocking).
    """
    import json as _json
    global _llm_router_api_key, _llm_router_base_url, _llm_router_client, _llm_router_model
    try:
        from openai import AsyncOpenAI
        model_name, base_url, api_key = _resolve_llm_router_runtime(
            config=config,
            model_config=model_config,
        )
        if not base_url or not api_key:
            return None
        if (
            _llm_router_client is None
            or _llm_router_model != model_name
            or _llm_router_base_url != base_url
            or _llm_router_api_key != api_key
        ):
            _llm_router_client = AsyncOpenAI(base_url=base_url, api_key=api_key)
            _llm_router_model = model_name
            _llm_router_base_url = base_url
            _llm_router_api_key = api_key
        resp = await _llm_router_client.chat.completions.create(
            model=_llm_router_model,
            messages=[
                {"role": "system", "content": _LLM_CLASSIFY_SYSTEM},
                {"role": "user", "content": question[:500]},
            ],
            max_tokens=30,
            temperature=0.0,
            timeout=5.0,
        )
        raw = resp.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            parsed = _json.loads(raw.strip())
            type_str = str(parsed.get("type", "")).strip().lower().replace("-", "_")
            confidence = float(parsed.get("confidence", 0.0))
            qt = QuestionType(type_str)
            return qt, confidence
        except (ValueError, KeyError, TypeError):
            # Fallback: maybe model output just the type name without JSON
            type_str = raw.strip().lower().replace("-", "_").split()[0]
            try:
                return QuestionType(type_str), 0.6  # partial confidence for non-JSON
            except ValueError:
                logger.debug(f"[llm_router] unrecognized label: {raw!r}")
                return None
    except Exception as e:
        logger.debug(f"[llm_router] classification failed: {e}")
        return None


def _classify_question_type_signals(question: str) -> QuestionType:
    """Signal-word fallback classifier (original v3.2 logic).

    Used when LLM classifier is unavailable or returns None.
    """
    q = question.lower()

    # v3.4: Realtime/news — must check before score loop to avoid FACTUAL/UNKNOWN misroute.
    # These questions require a search-capable model; deep pipeline gives stale answers.
    if _match_any(q, _REALTIME_SIGNALS):
        # WP13-R1: 复杂分析题碰巧提到日期/政策时，不截断为纯实时
        _analytical_co = _count_matches(q, _ANALYTICAL_SIGNALS)
        if _analytical_co >= 2 or (len(question) > 200 and _has_complexity(question)):
            pass  # fall through to score loop — will likely score ANALYTICAL
        else:
            return QuestionType.REALTIME

    # 1. Hard-match BEST_SINGLE types first (high-confidence patterns)
    if _match_any(q, _CREATIVE_SIGNALS):
        return QuestionType.CREATIVE
    if _match_any(q, _WRITING_SIGNALS):
        return QuestionType.WRITING
    if _match_any(q, _CODING_SIGNALS):
        return QuestionType.CODING
    if _match_any(q, _MATH_SIGNALS):
        return QuestionType.MATH

    # 2. Score all remaining categories
    scores = {
        QuestionType.REASONING: _count_matches(q, _REASONING_SIGNALS),
        QuestionType.TECHNICAL: _count_matches(q, _TECHNICAL_SIGNALS),
        QuestionType.META_COGNITION: _count_matches(q, _META_COGNITION_SIGNALS),
        QuestionType.CULTURAL: _count_matches(q, _CULTURAL_SIGNALS),
        QuestionType.CONTROVERSIAL: _count_matches(q, _CONTROVERSIAL_SIGNALS),
        QuestionType.ANALYTICAL: _count_matches(q, _ANALYTICAL_SIGNALS),
        QuestionType.FACTUAL: _count_matches(q, _FACTUAL_SIGNALS),
    }

    # 3. Pick the category with the highest score
    best_type = max(scores, key=scores.get)  # type: ignore
    best_score = scores[best_type]

    if best_score == 0:
        if len(question) > 200 and _has_complexity(question):
            return QuestionType.ANALYTICAL
        return QuestionType.UNKNOWN

    # 4. v3.2 tie-breaking: RACE types win if score strictly > all AGGR types
    race_types = {QuestionType.FACTUAL, QuestionType.CONTROVERSIAL, QuestionType.CULTURAL}
    aggr_priority = [QuestionType.TECHNICAL, QuestionType.REASONING,
                     QuestionType.META_COGNITION, QuestionType.ANALYTICAL]
    race_priority = [QuestionType.CONTROVERSIAL, QuestionType.CULTURAL, QuestionType.FACTUAL]

    best_aggr_score = max(scores[t] for t in aggr_priority)
    best_race_score = max(scores[t] for t in race_types)

    if best_race_score > best_aggr_score:
        for rtype in race_priority:
            if scores[rtype] == best_race_score:
                return rtype

    if best_aggr_score >= 1:
        for atype in aggr_priority:
            if scores[atype] == best_aggr_score:
                return atype

    return best_type


def _classify_question_type(question: str) -> QuestionType:
    """Classify question type for smart aggregation routing.

    v3.3: Two-layer classification:
      Layer 1 (sync, always runs): signal-word scoring — zero latency
      Layer 2 (async, optional):  LLM zero-shot via gemini_3_flash — higher accuracy

    The sync path is always used for the immediate RouteDecision.
    The async LLM path is exposed via classify_question_type_async() for callers
    that can await it (e.g. orchestrator fan-out phase).

    Design principles:
      - Signal words must be GENERAL patterns, not specific to any test set
      - Score ALL categories, pick highest score (not first-match)
      - UNKNOWN → full aggregation is a SAFE fallback (costs more but quality OK)
    """
    return _classify_question_type_signals(question)


async def classify_question_type_async(
    question: str,
    config: AppConfig | None = None,
) -> QuestionType:
    """Async entry point: try LLM classifier first, fall back to signal words.

    v3.3 confidence-aware logic:
      - LLM confidence >= 0.70 → use LLM result (overrides signal-word)
      - LLM confidence <  0.70 → keep signal-word result (conservative)
      - LLM unavailable/error  → signal-word result

    Call this from the orchestrator fan-out phase (parallel with contributor calls).
    Returns a QuestionType — never raises.
    """
    signal_result = _classify_question_type_signals(question)
    llm_result = await _llm_classify_question_type_async(question, config=config)

    if llm_result is None:
        logger.debug(f"[llm_router] LLM unavailable, using signal-word: {signal_result.value!r}")
        return signal_result

    qt, confidence = llm_result
    if confidence >= _LLM_CLASSIFY_MIN_CONFIDENCE:
        if qt != signal_result:
            logger.info(
                f"[llm_router] override signal-word {signal_result.value!r} "
                f"→ {qt.value!r} (confidence={confidence:.2f})"
            )
        else:
            logger.debug(f"[llm_router] confirmed {qt.value!r} (confidence={confidence:.2f})")
        return qt
    else:
        logger.info(
            f"[llm_router] low confidence ({confidence:.2f}<{_LLM_CLASSIFY_MIN_CONFIDENCE}), "
            f"keeping signal-word {signal_result.value!r} over LLM {qt.value!r}"
        )
        return signal_result


# Casual topics — suppress false Deep matches on simple questions
_CASUAL_TOPICS = [
    r"心情", r"感觉怎么样", r"开心", r"难过", r"无聊",
    r"喜欢", r"讨厌", r"好不好", r"好看", r"好听", r"好吃",
    r"天气", r"吃什么", r"怎么样了", r"怎么了",
    r"你好", r"嗯", r"哈哈", r"谢谢", r"再见",
]

# Complexity indicators — validate that long questions deserve Deep mode
_COMPLEXITY_INDICATORS = [
    r"[？?].*[？?]",               # Multiple question marks
    r"(架构|算法|原理|系统|协议|机制|引擎|编译|分布式|并发|缓存|索引)",
    r"(但是|然而|因此|所以|如果.*那么|尽管|虽然|however|therefore|because|although)",
    r"(1[.、]|第[一二三]|首先|其次|①|一方面)",  # Structured
    r"(vs|对比|区别|差异|优劣|trade.?off|权衡)",
    r"(分析|评估|论证|推导|证明|验证|解释.*原因)",
    r"(经济|政策|法律|哲学|心理学|社会学|历史|理论|方法论)",
]

# v2.0: Mode → default OutputDepth binding
_MODE_OUTPUT_DEPTH: dict[Mode, OutputDepth] = {
    Mode.LIGHT: OutputDepth.LEVEL_1,
    Mode.DEEP: OutputDepth.LEVEL_2,
    Mode.RESEARCH: OutputDepth.LEVEL_3,
    Mode.SOCRATIC: OutputDepth.LEVEL_3,  # Socratic always full visibility
}


def _match_any(text: str, patterns: list[str]) -> bool:
    """Check if text matches any of the regex patterns."""
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return True
    return False


def _count_matches(text: str, patterns: list[str]) -> int:
    """Count how many patterns match."""
    return sum(1 for p in patterns if re.search(p, text, re.IGNORECASE))


def _is_casual(question: str) -> bool:
    """Detect casual/conversational questions that shouldn't trigger Deep."""
    q = question.strip()
    return len(q) <= 30 and _match_any(q, _CASUAL_TOPICS)


def _has_complexity(question: str) -> bool:
    """Check if a long question has genuine analytical complexity."""
    return _count_matches(question, _COMPLEXITY_INDICATORS) >= 1


def route(
    question: str,
    session_context: list[Turn] | None = None,
    query_id: str = "",
) -> RouteDecision:
    """
    Route a question to the appropriate mode, web search, and critique settings.

    Phase 0: Pure rule-based. No model calls, no latency overhead.
    """
    q = question.strip()
    q_lower = q.lower()
    q_len = len(q)

    # ── Mode selection ──────────────────────────────────────
    if _match_any(q_lower, _RESEARCH_SIGNALS):
        mode = Mode.RESEARCH
    elif _match_any(q_lower, _DEEP_SIGNALS):
        # Suppress false positives: "为什么今天心情不好" should stay Light
        mode = Mode.LIGHT if _is_casual(q) else Mode.DEEP
    elif q_len > 300 and _has_complexity(q):
        # Long questions need genuine complexity indicators to trigger Deep
        mode = Mode.DEEP
    else:
        mode = Mode.LIGHT

    # Boost to Deep if question has follow-up context suggesting depth
    if session_context and len(session_context) >= 3 and mode == Mode.LIGHT:
        # User has been asking follow-ups → likely needs depth
        mode = Mode.DEEP

    # ── Web search decision ─────────────────────────────────
    if _match_any(q_lower, _REALTIME_SIGNALS):
        web_search = True
    elif _match_any(q_lower, _OFFLINE_SIGNALS):
        web_search = False
    else:
        web_search = True  # default: enable (better safe than sorry)

    # ── Critique decision ───────────────────────────────────
    if mode == Mode.DEEP:
        critique = True  # Deep: always on
    elif mode == Mode.RESEARCH:
        critique = _match_any(q_lower, _ASSUMPTION_SIGNALS)  # medium threshold
    else:
        critique = _match_any(q_lower, _FALSE_PREMISE_SIGNALS)  # high threshold

    # ── v2.0: Intent & OutputDepth ───────────────────────────
    # Phase 1: auto routing always produces ANSWER intent.
    # Socratic (GROWTH) is explicit-only via --mode socratic.
    intent = Intent.ANSWER
    output_depth = _MODE_OUTPUT_DEPTH.get(mode, OutputDepth.LEVEL_1)

    # ── v2.4: Question type classification ─────────────────
    question_type = _classify_question_type(q)

    decision = RouteDecision(
        mode=mode,
        web_search_enabled=web_search,
        critique_enabled=critique,
        intent=intent,
        output_depth=output_depth,
        question_type=question_type,
    )

    # v2.3: Log routing decision for future model-based router training
    _log_routing_decision(q, decision, q_len, query_id=query_id)

    return decision


# ============================================================
# Routing decision JSONL logging
# Single background consumer thread + queue (no thread-per-write).
# Pattern aligned with audit_log.py _AuditLogWriter.
# ============================================================

_routing_log_queue: queue.Queue[str | None] = queue.Queue(maxsize=10000)


def _routing_log_consumer() -> None:
    """Background daemon: drain queue and append to JSONL file."""
    while True:
        try:
            item = _routing_log_queue.get(timeout=1)
        except queue.Empty:
            continue
        if item is None:
            _drain_routing_log_queue()
            break
        _write_routing_log_line(item)


def _drain_routing_log_queue() -> None:
    """Flush remaining entries on shutdown."""
    while not _routing_log_queue.empty():
        try:
            item = _routing_log_queue.get_nowait()
            if item is not None:
                _write_routing_log_line(item)
        except queue.Empty:
            break


def _write_routing_log_line(line: str) -> None:
    try:
        _ROUTING_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_ROUTING_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logger.error(f"Routing log write failed: {e}")


_routing_log_thread = threading.Thread(
    target=_routing_log_consumer, daemon=True, name="routing-log-writer"
)
_routing_log_thread.start()


def _flush_routing_log() -> None:
    """Shutdown hook: send sentinel and wait for consumer to drain."""
    _routing_log_queue.put(None)
    _routing_log_thread.join(timeout=5)


atexit.register(_flush_routing_log)


def _log_routing_decision(question: str, decision: RouteDecision, q_len: int, query_id: str = "") -> None:
    """Append routing decision to JSONL log via background consumer."""
    entry = {
        "ts": time.time(),
        "query_id": query_id,
        "question_preview": question[:100],
        "question_len": q_len,
        "mode": decision.mode.value,
        "web_search": decision.web_search_enabled,
        "critique": decision.critique_enabled,
        "question_type": decision.question_type.value,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        _routing_log_queue.put_nowait(line)
    except queue.Full:
        logger.warning("Routing log queue full, dropping entry")


def enrich_routing_log(quality_gate_result: str, confidence: float, query_id: str = "") -> None:
    """Append outcome data to the routing log.

    Called by orchestrator after pipeline completes, so the JSONL log
    has both the routing decision AND the outcome — required for
    training a model-based router (supervised learning needs labels).
    query_id links this outcome back to the routing decision entry.
    """
    entry = {
        "ts": time.time(),
        "query_id": query_id,
        "type": "outcome",
        "quality_gate_result": quality_gate_result,
        "confidence": round(confidence, 3),
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        _routing_log_queue.put_nowait(line)
    except queue.Full:
        logger.warning("Routing log queue full, dropping entry")
