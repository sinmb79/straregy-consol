"""
AI client for the 22B Strategy Engine — Phase 4.

Uses OpenClaw agent via OpenAI-compatible HTTP API.
  Base URL : http://127.0.0.1:18789/v1  (default)
  Model    : openclaw:main  (or configured OPENCLAW_AGENT_ID)

IMPORTANT:
  - AI is NEVER used for execution decisions.
  - Used ONLY for: analysis, interpretation, recommendations.
  - All trade execution remains deterministic and rule-based.
"""

import base64
import logging
import mimetypes
from typing import Optional

logger = logging.getLogger(__name__)

MAX_TOKENS_REGIME = 2000
MAX_TOKENS_WEEKLY = 4000
MAX_TOKENS_DAILY  = 1500

DEFAULT_SYSTEM = (
    "You are a quantitative trading analyst assistant for the 22B Strategy Engine. "
    "Your role is analysis and interpretation only. You never make execution decisions. "
    "Be concise, data-driven, and actionable in your responses."
)


class ClaudeClient:
    """
    AI client that calls OpenClaw agent via OpenAI-compatible API.

    Graceful degradation:
      - If OpenClaw is unreachable, is_available() returns False.
      - All analyze() calls return a placeholder string instead of raising.
    """

    def __init__(self, base_url: str, token: str, agent_id: str, ai_enabled: bool = True) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._agent_id = agent_id
        self._ai_enabled = ai_enabled
        self._client = None

        if not self._ai_enabled:
            logger.info("AI_ENABLED=false — AI features disabled")
            return

        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=self._token or "no-key",
                base_url=f"{self._base_url}/v1",
            )
            logger.info(
                "ClaudeClient (OpenClaw) initialised — base=%s agent=%s",
                self._base_url, self._agent_id,
            )
        except ImportError:
            logger.warning("openai package not installed — AI features disabled. Run: pip install openai")
        except Exception as exc:
            logger.warning("ClaudeClient init failed: %s", exc)

    async def is_available(self) -> bool:
        return self._client is not None and self._ai_enabled

    async def analyze(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_tokens: int = MAX_TOKENS_REGIME,
    ) -> str:
        if not await self.is_available():
            return "[AI analysis unavailable — OpenClaw not connected or AI_ENABLED=false]"

        system_prompt = system or DEFAULT_SYSTEM
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": prompt},
        ]

        try:
            resp = await self._client.chat.completions.create(
                model=f"openclaw:{self._agent_id}",
                messages=messages,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content
            return content or "[AI returned empty response]"
        except Exception as exc:
            logger.error("OpenClaw API call failed: %s", exc)
            return f"[AI analysis temporarily unavailable: {type(exc).__name__}]"

    async def analyze_regime(self, prompt: str) -> str:
        return await self.analyze(prompt, max_tokens=MAX_TOKENS_REGIME)

    async def analyze_weekly(self, prompt: str) -> str:
        return await self.analyze(prompt, max_tokens=MAX_TOKENS_WEEKLY)

    async def analyze_daily(self, prompt: str) -> str:
        return await self.analyze(prompt, max_tokens=MAX_TOKENS_DAILY)

    async def analyze_image(
        self,
        image_bytes: bytes,
        text_prompt: str,
        mime_type: str = "image/png",
        system: Optional[str] = None,
        max_tokens: int = 2000,
    ) -> str:
        """
        Send an image + text prompt to the AI via OpenAI-compatible vision API.
        image_bytes : raw image file bytes
        mime_type   : 'image/png' | 'image/jpeg' | 'image/webp'
        Returns the AI response string, or an error placeholder.
        """
        if not await self.is_available():
            return "[AI unavailable — OpenClaw not connected or AI_ENABLED=false]"

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"

        system_prompt = system or DEFAULT_SYSTEM
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": "high"},
                    },
                    {"type": "text", "text": text_prompt},
                ],
            },
        ]

        try:
            resp = await self._client.chat.completions.create(
                model=f"openclaw:{self._agent_id}",
                messages=messages,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content
            return content or "[AI returned empty response]"
        except Exception as exc:
            logger.error("OpenClaw vision API call failed: %s", exc)
            return f"[Vision analysis unavailable: {type(exc).__name__}: {exc}]"
