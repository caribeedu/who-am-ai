"""Claude via the Anthropic API, with structured output, a spend ledger and the monthly cap.

No fallback: if the key is missing, the cap is reached or the API fails, the run stops and says so."""

from __future__ import annotations

import json
from typing import Any

from .store import Store, now

PRICES = {  # USD per million tokens: input, output, cache read, cache write
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.50),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.00),
}
KEYCHAIN = ("who-am-ai", "anthropic_api_key")


class LLMStopped(Exception):
    """The run cannot use the LLM (no key, cap reached, API refused). Nothing is interpreted without it."""


def api_key() -> str | None:
    import keyring
    from keyring.errors import KeyringError
    try:
        return keyring.get_password(*KEYCHAIN)
    except KeyringError:
        return None


def cost(model: str, usage: Any) -> float:
    p = PRICES.get(model, PRICES["claude-sonnet-5-5"])
    return ((getattr(usage, "input_tokens", 0) or 0) * p[0] + (getattr(usage, "output_tokens", 0) or 0) * p[1]
            + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p[2]
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p[3]) / 1_000_000


class LLM:
    def __init__(self, store: Store, cap_usd: float):
        self.store = store
        self.cap_usd = cap_usd
        self._client = None

    def client(self):
        if self._client is None:
            key = api_key()
            if not key:
                raise LLMStopped("no Anthropic API key in the system keychain (run `whoamai key`)")
            import anthropic
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def structured(self, step: str, model: str, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
        spent = self.store.month_spend()
        if spent >= self.cap_usd:
            raise LLMStopped(f"monthly cap reached (US${spent:.2f} of US${self.cap_usd:.2f})")
        import anthropic
        params = {"model": model, "max_tokens": max_tokens,
                  "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                  "messages": [{"role": "user", "content": user}],
                  "output_config": {"format": {"type": "json_schema", "schema": schema}}}
        try:
            message = self.client().messages.create(**params)
        except anthropic.APIError as exc:
            self.store.x("INSERT INTO llm_calls (ts, step, model, ok, error) VALUES (?, ?, ?, 0, ?)",
                         (now(), step, model, str(exc)[:300]))
            raise LLMStopped(f"API error: {exc}") from exc
        usage = message.usage
        self.store.x("""INSERT INTO llm_calls (ts, step, model, input_tokens, output_tokens, cost_usd, ok)
                        VALUES (?, ?, ?, ?, ?, ?, 1)""",
                     (now(), step, model, usage.input_tokens, usage.output_tokens, cost(model, usage)))
        text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
        return json.loads(text)


def obj(properties: dict, required: list[str] | None = None) -> dict:
    """Closed JSON Schema object (every property required, nothing extra)."""
    return {"type": "object", "properties": properties, "required": required or list(properties),
            "additionalProperties": False}
