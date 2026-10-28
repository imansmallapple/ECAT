"""LLM Judge — supports Anthropic SDK and OpenAI-compatible SDK for soft scoring."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

log = logging.getLogger(__name__)

_VERDICT_JSON_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "score": {"type": "number"},
                "rationale": {"type": "string"},
                "issues": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["score", "rationale", "issues"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class JudgeVerdict:
    """Structured result from an LLM judge call."""

    score: float
    rationale: str
    raw: str = ""


class JudgeClient:
    """Thin wrapper supporting Anthropic SDK and OpenAI-compatible SDK.

    Dispatches on *runtime*:
    - ``anthropic_sdk`` (default): uses ``anthropic.Anthropic`` — for Claude models.
    - ``openai_sdk``: uses ``openai.OpenAI`` with optional *base_url* — for DeepSeek,
      OpenAI, and any OpenAI-compatible provider.

    The ``anthropic_sdk`` path is unchanged from the original implementation so
    existing Claude-based deployments are not affected.
    """

    def __init__(
        self,
        *,
        runtime: Literal["anthropic_sdk", "openai_sdk"] = "anthropic_sdk",
        model: str = "claude-sonnet-4-6",
        api_key_env: str = "ANTHROPIC_API_KEY",
        base_url: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self.runtime = runtime
        self.model = model
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.max_tokens = max_tokens

        api_key = os.environ.get(self.api_key_env, "")
        # Typed as Any to avoid hard dependency on both SDKs at import time.
        self._client: Any = None

        if runtime == "openai_sdk":
            import openai

            kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": 4, "timeout": 60.0}
            if base_url:
                # OpenAI SDK expects the full versioned URL (.../v1); append if absent.
                url = base_url.rstrip("/")
                if not url.endswith("/v1"):
                    url = f"{url}/v1"
                kwargs["base_url"] = url
            self._client = openai.OpenAI(**kwargs)
        else:
            import anthropic

            kwargs = {"api_key": api_key, "max_retries": 4, "timeout": 60.0}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = anthropic.Anthropic(**kwargs)

    def judge(self, system: str, user_prompt: str) -> JudgeVerdict:
        """Send a judge prompt and return a structured verdict.

        On any failure returns score=0.5 so the plugin falls back gracefully.
        """
        try:
            raw = (
                self._call_openai(system, user_prompt)
                if self.runtime == "openai_sdk"
                else self._call_anthropic(system, user_prompt)
            )
            return _parse_verdict(raw)
        except Exception as exc:
            log.warning("LLM judge call failed: %s", exc)
            return JudgeVerdict(score=0.5, rationale=f"judge error: {exc}")

    def _call_anthropic(self, system: str, user_prompt: str) -> str:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
        for block in response.content:
            if hasattr(block, "text"):
                return str(block.text)
        return ""

    def _call_openai(self, system: str, user_prompt: str) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ]
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                response_format=_VERDICT_JSON_SCHEMA,
                messages=messages,
            )
        except Exception:
            # json_schema not supported by this provider/model — fall back to json_object
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"},
                messages=messages,
            )
        return response.choices[0].message.content or ""


def _parse_verdict(raw: str) -> JudgeVerdict:
    """Extract score and rationale from the LLM's JSON response."""
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        preview = (raw[:300] + "…") if len(raw) > 300 else raw
        log.warning(
            "No JSON in judge response; using default score. raw[:300]=%r",
            preview,
        )
        return JudgeVerdict(score=0.5, rationale="unparseable response", raw=raw)

    try:
        data = json.loads(match.group())
        score = float(data.get("score", 0.5))
        score = max(0.0, min(1.0, score))
        rationale = str(data.get("rationale", ""))
        return JudgeVerdict(score=score, rationale=rationale, raw=raw)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        log.warning("Failed to parse judge JSON: %s", exc)
        return JudgeVerdict(score=0.5, rationale=f"parse error: {exc}", raw=raw)
