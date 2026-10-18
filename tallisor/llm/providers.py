"""Minimal, dependency-free clients for hosted language models.

The model is optional. With no API key (the default) Tallisor uses
deterministic explanations, so the product works offline and every test is
reproducible. To turn a model on, put one key in a .env file in the project
folder (or export it):

    ANTHROPIC_API_KEY=...   or   OPENAI_API_KEY=...   or   GEMINI_API_KEY=...
    TALLISOR_LLM_PROVIDER=anthropic | openai | gemini   (optional)
    TALLISOR_LLM_MODEL=<model name>                      (optional)

Every call asks for JSON only. Any network error, timeout or malformed reply
returns None and the caller falls back to the deterministic path.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
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


KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4o-mini", "gemini": "gemini-2.5-flash"}


def _dotenv() -> dict:
    """Read KEY=value lines from a private .env file, if there is one.

    Looked for in the project folder and in the user's Tallisor workspace.
    Values already set in the environment win. The file is never committed
    (.gitignore lists it) and never logged.
    """
    found = {}
    for path in (Path(__file__).resolve().parents[2] / ".env",
                 Path(os.getenv("TALLISOR_WORKSPACE", Path.home() / ".tallisor")).expanduser() / ".env"):
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    found.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'"))
        except OSError:
            continue
    return found


def _setting(name: str, dotenv: dict) -> str:
    return (os.getenv(name) or dotenv.get(name) or "").strip()


def from_env() -> LLM:
    """The configured model, or NullLLM (fixed-rule explanations) if none.

    The provider is TALLISOR_LLM_PROVIDER if set; otherwise the first provider
    whose API key is present (Anthropic, OpenAI, Gemini).
    """
    dotenv = _dotenv()
    provider = _setting("TALLISOR_LLM_PROVIDER", dotenv).lower()
    if not provider:
        provider = next((p for p, k in KEYS.items() if _setting(k, dotenv)), "")
    key = _setting(KEYS.get(provider, ""), dotenv) if provider in KEYS else ""
    if not key:
        return NullLLM()
    model = _setting("TALLISOR_LLM_MODEL", dotenv) or DEFAULT_MODELS[provider]
    return {"anthropic": AnthropicLLM, "openai": OpenAILLM, "gemini": GeminiLLM}[provider](model, key)


class ScriptedLLM(LLM):
    """Test double that returns pre-set replies, including malicious ones."""
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete_json(self, system, user):
        self.prompts.append((system, user))
        return self.replies.pop(0) if self.replies else None
