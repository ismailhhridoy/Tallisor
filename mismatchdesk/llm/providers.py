"""Minimal, dependency-free clients for hosted language models.

The model is optional. With MD_LLM_PROVIDER unset (the default) Mismatch Desk
uses deterministic explanations, so the product works offline and every test
is reproducible. Set:

    MD_LLM_PROVIDER = anthropic | openai | gemini
    MD_LLM_MODEL    = model name offered by that provider
    ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY

Every call asks for JSON only. Any network error, timeout or malformed reply
returns None and the caller falls back to the deterministic path.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional

TIMEOUT = 30


class LLM:
    name = "none"
    model = ""

    def complete_json(self, system: str, user: str) -> Optional[dict]:
        return None


class NullLLM(LLM):
    """No model: callers use their deterministic fallback."""


def _post(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (fixed https endpoints)
        return json.loads(resp.read().decode())


def _parse_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class AnthropicLLM(LLM):
    name = "anthropic"

    def __init__(self, model: str, key: str):
        self.model, self.key = model, key

    def complete_json(self, system, user):
        try:
            out = _post("https://api.anthropic.com/v1/messages",
                        {"x-api-key": self.key, "anthropic-version": "2023-06-01"},
                        {"model": self.model, "max_tokens": 600, "temperature": 0, "system": system,
                         "messages": [{"role": "user", "content": user}]})
            return _parse_json("".join(b.get("text", "") for b in out.get("content", [])))
        except Exception:
            return None


class OpenAILLM(LLM):
    name = "openai"

    def __init__(self, model: str, key: str):
        self.model, self.key = model, key

    def complete_json(self, system, user):
        try:
            out = _post("https://api.openai.com/v1/chat/completions",
                        {"authorization": f"Bearer {self.key}"},
                        {"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            return _parse_json(out["choices"][0]["message"]["content"])
        except Exception:
            return None


class GeminiLLM(LLM):
    name = "gemini"

    def __init__(self, model: str, key: str):
        self.model, self.key = model, key

    def complete_json(self, system, user):
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
            out = _post(url, {"x-goog-api-key": self.key},
                        {"systemInstruction": {"parts": [{"text": system}]},
                         "contents": [{"role": "user", "parts": [{"text": user}]}],
                         "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}})
            parts = out["candidates"][0]["content"]["parts"]
            return _parse_json("".join(p.get("text", "") for p in parts))
        except Exception:
            return None


def from_env() -> LLM:
    provider = os.getenv("MD_LLM_PROVIDER", "").strip().lower()
    model = os.getenv("MD_LLM_MODEL", "").strip()
    if provider == "anthropic" and os.getenv("ANTHROPIC_API_KEY"):
        return AnthropicLLM(model or "claude-sonnet-4-5", os.environ["ANTHROPIC_API_KEY"])
    if provider == "openai" and os.getenv("OPENAI_API_KEY"):
        return OpenAILLM(model or "gpt-4o-mini", os.environ["OPENAI_API_KEY"])
    if provider == "gemini" and os.getenv("GEMINI_API_KEY"):
        return GeminiLLM(model or "gemini-2.5-flash", os.environ["GEMINI_API_KEY"])
    return NullLLM()


class ScriptedLLM(LLM):
    """Test double that returns pre-set replies, including malicious ones."""
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete_json(self, system, user):
        self.prompts.append((system, user))
        return self.replies.pop(0) if self.replies else None
