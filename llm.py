"""Thin LLM client wrapper supporting OpenAI **and** OpenRouter (both are
OpenAI-compatible HTTP APIs).

Provider selection
------------------
  * ``LLM_PROVIDER`` explicitly picks ``openai`` or ``openrouter``.
  * If unset, OpenRouter is used when ``OPENROUTER_API_KEY`` is present,
    otherwise OpenAI is used.

OpenAI
    OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL (default: gpt-4o-mini)

OpenRouter
    OPENROUTER_API_KEY / OPENROUTER_BASE_URL (default: https://openrouter.ai/api/v1)
    OPENROUTER_MODEL (default: openai/gpt-4o-mini)
    Optional: OPENROUTER_REFERER, OPENROUTER_APP_NAME (sent as HTTP-Referer/X-Title)

If no API key is configured, ``available`` is False and the agents use the
deterministic fallback logic instead, so the full demo still runs offline.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class LLMClient:
    def __init__(self) -> None:
        explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
        or_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        oai_key = os.getenv("OPENAI_API_KEY", "").strip()

        if explicit in ("openrouter", "openai"):
            self.provider = explicit
        else:
            self.provider = "openrouter" if or_key else "openai"

        self._extra_headers: Optional[Dict[str, str]] = None

        if self.provider == "openrouter":
            self.api_key = or_key
            self.base_url = os.getenv(
                "OPENROUTER_BASE_URL", OPENROUTER_DEFAULT_BASE_URL).strip()
            self.model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()
            self._extra_headers = {
                "HTTP-Referer": os.getenv("OPENROUTER_REFERER",
                                          "http://localhost:5000").strip(),
                "X-Title": os.getenv("OPENROUTER_APP_NAME",
                                     "GraphHacks").strip(),
            }
        else:
            self.api_key = oai_key
            self.base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
            self.model = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()

        self._client = None
        if self.api_key:
            from openai import OpenAI
            kwargs: Dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            if self._extra_headers:
                kwargs["default_headers"] = self._extra_headers
            self._client = OpenAI(**kwargs)

    @property
    def available(self) -> bool:
        return self._client is not None

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.4,
             max_tokens: int = 800) -> str:
        if not self._client:
            raise RuntimeError("LLM is not configured (set an API key).")
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()

    def chat_json(self, messages: List[Dict[str, str]],
                  temperature: float = 0.2) -> Any:
        text = self.chat(messages, temperature=temperature)
        return extract_json(text)


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from an LLM reply (handles code fences)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            if start == -1:
                continue
            depth = 0
            for i in range(start, len(text)):
                if text[i] == opener:
                    depth += 1
                elif text[i] == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break
        raise ValueError(f"Could not parse JSON from LLM output: {text[:300]}")
