"""
FailureMonitor — 模型熔断与告警服务 (v1.0)

订阅 ModelCallFailed 事件，维护内存滑动窗口。
当某模型在 5 分钟内连续失败 ≥3 次，标记为 DEGRADED 15 分钟，
并通过 Webhook 发送告警。

设计约束：
  - 纯内存状态（重启后清零，可接受）
  - 零外部依赖（asyncio + collections + aiohttp/urllib）
  - 不阻塞主管道（fire-and-forget）
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.error
from collections import defaultdict, deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agoracle.domain.events import ModelCallFailed

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 300          # 5 分钟滑动窗口
_FAIL_THRESHOLD = 3            # 窗口内失败 ≥3 次触发熔断
_DEGRADE_DURATION = 900        # 熔断时长 15 分钟（秒）
_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")  # 飞书/企微/Telegram Webhook


class FailureMonitor:
    """
    EventBus 订阅者：ModelCallFailed → 熔断 + 告警。

    Usage:
        monitor = FailureMonitor()
        event_bus.subscribe(ModelCallFailed, monitor.on_model_call_failed)

    Orchestrator 在 fan_out 前调用 is_degraded(model_id) 跳过故障模型。
    """

    def __init__(self) -> None:
        self._failure_windows: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=100)
        )
        self._degraded_until: dict[str, float] = {}

    def is_degraded(self, model_id: str) -> bool:
        """主管道调用：判断模型是否处于熔断状态。"""
        until = self._degraded_until.get(model_id, 0)
        if until and time.time() < until:
            return True
        if until and time.time() >= until:
            self._degraded_until.pop(model_id, None)
            logger.info(f"[FailureMonitor] {model_id} 熔断解除，恢复正常")
        return False

    async def on_model_call_failed(self, event: "ModelCallFailed") -> None:
        """EventBus handler — fire-and-forget，不阻塞主管道。"""
        model_id = event.model_id
        now = time.time()

        window = self._failure_windows[model_id]
        window.append(now)

        recent = [t for t in window if now - t <= _WINDOW_SECONDS]
        self._failure_windows[model_id] = deque(recent, maxlen=100)

        logger.warning(
            f"[FailureMonitor] {model_id} 失败 ({event.error[:80]}) "
            f"| 窗口内失败次数={len(recent)}"
        )

        if len(recent) >= _FAIL_THRESHOLD and model_id not in self._degraded_until:
            self._degraded_until[model_id] = now + _DEGRADE_DURATION
            logger.error(
                f"[FailureMonitor] 🔴 {model_id} 触发熔断，"
                f"将跳过 {_DEGRADE_DURATION//60} 分钟"
            )
            asyncio.create_task(self._send_alert(model_id, len(recent), event.error))

    async def _send_alert(self, model_id: str, fail_count: int, last_error: str) -> None:
        """异步发送 Webhook 告警（仅在配置了 ALERT_WEBHOOK_URL 时生效）。"""
        if not _WEBHOOK_URL:
            logger.info(f"[FailureMonitor] 未配置 ALERT_WEBHOOK_URL，跳过告警推送")
            return
        try:
            import urllib.request
            import json as _json
            payload = _json.dumps({
                "msg_type": "text",
                "content": {
                    "text": (
                        f"🔴 Synthora 模型熔断告警\n"
                        f"模型: {model_id}\n"
                        f"5分钟内失败次数: {fail_count}\n"
                        f"最近错误: {last_error[:200]}\n"
                        f"熔断时长: {_DEGRADE_DURATION//60} 分钟"
                    )
                }
            }).encode()
            req = urllib.request.Request(
                _WEBHOOK_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            await asyncio.to_thread(urllib.request.urlopen, req, timeout=5)
            logger.info(f"[FailureMonitor] 告警已推送: {model_id}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning(f"[FailureMonitor] Webhook 推送失败: {e}")

    def get_status(self) -> dict:
        """返回当前熔断状态摘要（供 /api/health 或管理接口使用）。"""
        now = time.time()
        degraded = {
            mid: int(until - now)
            for mid, until in self._degraded_until.items()
            if until > now
        }
        return {
            "degraded_models": degraded,
            "monitored_models": list(self._failure_windows.keys()),
        }
