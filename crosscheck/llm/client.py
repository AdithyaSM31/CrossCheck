"""One LLM interface, two API shapes.

``openai`` covers any OpenAI-compatible /chat/completions endpoint — Groq, OpenAI,
OpenRouter, Together, local Ollama. ``anthropic`` covers the Messages API. Which one a role
uses is a .env line.

Two things here earn their keep beyond plumbing:

* **Rate-limit survival.** Groq's free tier limits tokens per minute, and a 1,300-block run
  will hit it repeatedly. 429s are retried with the server's own ``retry-after`` when it
  offers one, and the concurrency pool is shared, so a limited run slows down instead of
  failing.
* **JSON that actually parses.** Open models emit prose around JSON, wrap it in fences, and
  trail commas. ``parse_json`` repairs the common damage rather than discarding the call,
  because a discarded call is a fact silently lost.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field

import httpx

from ..config import RoleConfig


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class Usage:
    calls: int = 0
    cached: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    rate_limited: int = 0
    seconds: float = 0.0

    def add(self, other: "Usage") -> None:
        for f in ("calls", "cached", "input_tokens", "output_tokens", "retries",
                  "rate_limited", "seconds"):
            setattr(self, f, getattr(self, f) + getattr(other, f))

    def summary(self) -> str:
        return (
            f"{self.calls} calls ({self.cached} cached), "
            f"{self.input_tokens:,} in / {self.output_tokens:,} out tokens, "
            f"{self.retries} retries, {self.rate_limited} rate-limited, "
            f"{self.seconds:.0f}s"
        )


@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- JSON repair
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(text: str) -> object:
    """Parse JSON from a model response, repairing the usual damage.

    Weaker models wrap JSON in prose or fences and leave trailing commas. Throwing the
    response away loses real facts, so recover what is recoverable and fail loudly otherwise.
    """
    if not text or not text.strip():
        raise LLMError("empty response")

    candidates: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)

    # Fall back to the outermost bracketed span, dropping any surrounding commentary.
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])

    last: Exception | None = None
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        for attempt in (cand, re.sub(r",(\s*[\]}])", r"\1", cand)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError as exc:
                last = exc
    raise LLMError(f"could not parse JSON: {last}") from last


# --------------------------------------------------------------------------- the client
class LLMClient:
    """Async client for one role. Shared concurrency pool and usage accounting."""

    def __init__(
        self,
        cfg: RoleConfig,
        *,
        concurrency: int = 4,
        timeout: float = 120.0,
        max_calls: int = 0,
    ) -> None:
        if not cfg.configured:
            raise LLMError(
                f"role '{cfg.role}' is not configured — set CROSSCHECK_"
                f"{cfg.role.upper()}_API_KEY and _MODEL in .env"
            )
        self.cfg = cfg
        self.usage = Usage()
        self.max_calls = max_calls
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ request shaping
    def _request(self, system: str, user: str, max_tokens: int, json_mode: bool):
        if self.cfg.provider == "anthropic":
            return (
                f"{self.cfg.base_url.rstrip('/')}/v1/messages",
                {
                    "x-api-key": self.cfg.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                {
                    "model": self.cfg.model,
                    "max_tokens": max_tokens,
                    "temperature": 0,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )

        body = {
            "model": self.cfg.model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return (
            f"{self.cfg.base_url.rstrip('/')}/chat/completions",
            {
                "Authorization": f"Bearer {self.cfg.api_key}",
                "content-type": "application/json",
            },
            body,
        )

    @staticmethod
    def _read(provider: str, data: dict) -> LLMResponse:
        if provider == "anthropic":
            text = "".join(
                b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
            )
            u = data.get("usage", {})
            return LLMResponse(
                text, u.get("input_tokens", 0), u.get("output_tokens", 0),
                data.get("model", ""), data,
            )
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        u = data.get("usage", {})
        return LLMResponse(
            text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
            data.get("model", ""), data,
        )

    # ------------------------------------------------------------------ the call
    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
        json_mode: bool = True,
        attempts: int = 5,
    ) -> LLMResponse:
        if self.max_calls and self.usage.calls >= self.max_calls:
            raise BudgetExceeded(
                f"call budget of {self.max_calls} reached (CROSSCHECK_MAX_CALLS)"
            )

        url, headers, body = self._request(system, user, max_tokens, json_mode)
        started = time.perf_counter()
        last: Exception | None = None

        async with self._sem:
            for attempt in range(attempts):
                try:
                    r = await self._client.post(url, headers=headers, json=body)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last = exc
                    self.usage.retries += 1
                    await asyncio.sleep(min(2**attempt, 20) + random.random())
                    continue

                if r.status_code == 200:
                    resp = self._read(self.cfg.provider, r.json())
                    self.usage.calls += 1
                    self.usage.input_tokens += resp.input_tokens
                    self.usage.output_tokens += resp.output_tokens
                    self.usage.seconds += time.perf_counter() - started
                    return resp

                if r.status_code == 429 or r.status_code >= 500:
                    # Prefer the server's own backoff hint; free tiers report exactly how
                    # long the token bucket needs.
                    wait = min(2**attempt, 30) + random.random()
                    hinted = r.headers.get("retry-after")
                    if hinted:
                        try:
                            wait = min(float(hinted) + 0.5, 90.0)
                        except ValueError:
                            pass
                    if r.status_code == 429:
                        self.usage.rate_limited += 1
                    self.usage.retries += 1
                    last = LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                    await asyncio.sleep(wait)
                    continue

                raise LLMError(f"HTTP {r.status_code}: {r.text[:400]}")

        self.usage.seconds += time.perf_counter() - started
        raise LLMError(f"failed after {attempts} attempts: {last}")

    async def complete_json(self, system: str, user: str, **kw) -> object:
        resp = await self.complete(system, user, **kw)
        return parse_json(resp.text)
