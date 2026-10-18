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
    last_error = ""

    def complete_json(self, system: str, user: str) -> Optional[dict]:
        return None

    def check(self) -> tuple[bool, str]:
        """One tiny request to confirm the key, model and network work."""
        if self.name == "none":
            return False, "No API key found. Add one to the .env file in the project folder."
        reply = self.complete_json('Reply with JSON only: {"ok": true}', "Connection test.")
        if reply and reply.get("ok") is True:
            return True, f"Connected to {self.name}, model {self.model}."
        return False, self.last_error or "The model replied, but not in the expected format."


def _explain(exc: Exception) -> str:
    """A readable reason for a failed call, without ever including the key."""
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read().decode() or "{}")
            msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
        except Exception:
            msg = ""
        hint = {401: "the API key is wrong or revoked", 403: "the key has no access to this model",
                404: "the model name was not found", 429: "rate limit or no credit left on the account",
                400: "the request was refused"}.get(exc.code, "the provider returned an error")
        return f"HTTP {exc.code}: {hint}." + (f" Provider says: {str(msg)[:200]}" if msg else "")
    if isinstance(exc, urllib.error.URLError):
        return f"Could not reach the provider ({exc.reason}). Check the internet connection."
    return f"{type(exc).__name__}: {str(exc)[:200]}"


class NullLLM(LLM):
    """No model: callers use their deterministic fallback."""


def _ssl_context():
    """Trusted certificates. Python from python.org on macOS ships without any,
    which makes every HTTPS call fail; certifi provides Mozilla's list."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _post(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as resp:  # noqa: S310 (fixed https endpoints)
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
        except Exception as exc:  # never raise: callers fall back to fixed rules
            self.last_error = _explain(exc)
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
        except Exception as exc:  # never raise: callers fall back to fixed rules
            self.last_error = _explain(exc)
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
        except Exception as exc:  # never raise: callers fall back to fixed rules
            self.last_error = _explain(exc)
            return None


KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4o-mini", "gemini": "gemini-3.8-flash"}


def _dotenv() -> dict:
    """Read KEY=value lines from a private .env file, if there is one.

    The key saved from the app's Settings (in the user's Tallisor workspace) comes first,
    then a .env file in the project folder.
    Values already set in the environment win. The file is never committed
    (.gitignore lists it) and never logged.
    """
    found = {}
    for path in (_workspace_env(), Path(__file__).resolve().parents[2] / ".env"):
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    found.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'"))
        except OSError:
            continue
    return found


def _workspace_env() -> Path:
    from ..workspace import root
    return root() / ".env"


def save_api_key(provider: str, key: str) -> None:
    """Store one provider key in the user's private workspace file (owner-only permissions).

    Any other provider keys there are removed, so exactly one model is used.
    """
    if provider not in KEYS:
        raise ValueError(f"Unknown AI provider: {provider}")
    key = key.strip()
    if len(key) < 10 or any(c.isspace() for c in key):
        raise ValueError("That does not look like an API key. Copy it again from the provider's website.")
    path = _workspace_env()
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = []
    if path.exists():
        keep = [l for l in path.read_text().splitlines()
                if not any(l.strip().removeprefix("export ").startswith(k + "=") for k in KEYS.values())
                and not l.strip().startswith("TALLISOR_LLM_")]
    keep += [f"TALLISOR_LLM_PROVIDER={provider}", f"{KEYS[provider]}={key}"]
    path.write_text("\n".join(keep) + "\n")
    os.chmod(path, 0o600)


def remove_api_key() -> None:
    path = _workspace_env()
    if path.exists():
        lines = [l for l in path.read_text().splitlines()
                 if not any(l.strip().removeprefix("export ").startswith(k + "=") for k in KEYS.values())
                 and not l.strip().startswith("TALLISOR_LLM_")]
        path.write_text("\n".join(lines) + ("\n" if lines else ""))


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
