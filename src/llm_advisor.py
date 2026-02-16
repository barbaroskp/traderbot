"""Optional LLM advisor – uses local Ollama to approve/reject/reduce signals.

When enabled, each trading signal is sent to the LLM with context (indicators,
recent PnL, open positions). The model replies APPROVE / REJECT / REDUCE.
This adds a reasoning layer on top of the scripted strategy.

Requirements:
  - Ollama installed on the server: https://ollama.com
  - A small model pulled: ollama pull llama3.2:3b
  - use_llm_advisor=true in config

If Ollama is down or times out, we fall back to APPROVE (script-only behavior).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from src.config import Settings
from src.logger import get_logger
from src.strategy import Signal

log = get_logger(__name__)


@dataclass
class LLMAdvice:
    action: str  # "approve" | "reject" | "reduce"
    reason: str = ""


def _build_prompt(signal: Signal, context: dict[str, Any]) -> str:
    """Build a short context for the LLM."""
    lines = [
        "You are a trading assistant. Reply with ONLY one word: APPROVE, REJECT, or REDUCE.",
        "Optional: add one short reason after a space.",
        "",
        "Signal:",
        f"  Symbol: {signal.symbol}  Side: {signal.side}",
        f"  Z-score (bps): {signal.z_score_bps:.1f}  RSI: {signal.rsi:.1f}",
        f"  MACD hist: {signal.macd_histogram:.6f}  BB%: {signal.bollinger_pct:.2f}",
        f"  Trend: {signal.trend_direction}  Confluence: {signal.confluence_score}/5",
        "",
        "Context:",
        f"  Open positions: {context.get('open_positions', 0)}",
        f"  Recent PnL (last 5): {context.get('recent_pnl_summary', 'N/A')}",
        f"  Balance: {context.get('balance', 0):.1f} USDT",
        "",
        "Reply (one word or word + reason):",
    ]
    return "\n".join(lines)


def _parse_response(text: str) -> LLMAdvice:
    """Parse LLM response into action + reason."""
    if not text or not text.strip():
        return LLMAdvice(action="approve", reason="empty")
    upper = text.strip().upper()
    reason = ""
    if " " in text.strip():
        parts = text.strip().split(None, 1)
        reason = parts[1].strip() if len(parts) > 1 else ""
    if "REJECT" in upper:
        return LLMAdvice(action="reject", reason=reason or "llm said reject")
    if "REDUCE" in upper:
        return LLMAdvice(action="reduce", reason=reason or "llm said reduce")
    return LLMAdvice(action="approve", reason=reason or "llm said approve")


class LLMAdvisor:
    """Calls local Ollama to get approve/reject/reduce for a signal."""

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._calls_this_cycle = 0

    def reset_cycle(self) -> None:
        """Call at start of each scheduler cycle."""
        self._calls_this_cycle = 0

    def _can_call(self) -> bool:
        if self._calls_this_cycle >= self.cfg.ollama_max_calls_per_cycle:
            return False
        return True

    async def advise(
        self,
        signal: Signal,
        context: dict[str, Any],
    ) -> LLMAdvice:
        """
        Ask the LLM whether to approve, reject, or reduce this signal.
        On timeout or error, returns approve (fallback to script-only).
        """
        if not self.cfg.use_llm_advisor:
            return LLMAdvice(action="approve", reason="llm disabled")
        if not self._can_call():
            return LLMAdvice(action="approve", reason="max calls reached")

        self._calls_this_cycle += 1
        prompt = _build_prompt(signal, context)
        url = f"{self.cfg.ollama_base_url.rstrip('/')}/api/generate"

        try:
            async with httpx.AsyncClient(timeout=self.cfg.ollama_timeout_seconds) as client:
                resp = await client.post(
                    url,
                    json={
                        "model": self.cfg.ollama_model,
                        "prompt": prompt,
                        "stream": False,
                    },
                )
            if resp.status_code != 200:
                log.warning("ollama non-200", extra={"status": resp.status_code, "body": resp.text[:200]})
                return LLMAdvice(action="approve", reason=f"ollama status {resp.status_code}")
            data = resp.json()
            text = data.get("response", "")
            advice = _parse_response(text)
            log.info(
                "llm advice",
                extra={
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "action": advice.action,
                    "reason": advice.reason[:80] if advice.reason else "",
                },
            )
            return advice
        except asyncio.TimeoutError:
            log.warning("ollama timeout", extra={"symbol": signal.symbol})
            return LLMAdvice(action="approve", reason="timeout")
        except Exception as exc:
            log.warning("ollama error", extra={"symbol": signal.symbol, "error": str(exc)})
            return LLMAdvice(action="approve", reason=str(exc)[:50])
