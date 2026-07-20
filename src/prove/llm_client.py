"""LLM transport abstraction — and the file that carries this project's Alibaba Cloud integration.

Every real model call in PROVE goes through `OpenAICompatClient` below, which targets **Alibaba
Cloud Model Studio (DashScope)** over its OpenAI-compatible endpoint:

    https://dashscope-intl.aliyuncs.com/compatible-mode/v1

The models are Qwen (`qwen-turbo` / `qwen-plus` extraction, `qwen-coder-plus` synthesis; see
`configs/default.yaml`), authenticated with `DASHSCOPE_API_KEY`. Live-run artifacts produced
through this client are committed under `evals/live_results/`.

Two implementations behind one interface:
  - FakeClient          — deterministic test/eval double; CI never touches a real API.
  - OpenAICompatClient  — openai SDK against the DashScope endpoint above. The base_url is config,
                          not code, so the same client also serves any other OpenAI-compatible
                          provider without a code change.

Cost math lives ONLY in estimate_cost() so token->dollar accounting has a single source.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class LLMResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""


def estimate_cost(model: str, tokens_in: int, tokens_out: int, costs: dict[str, Any]) -> float:
    """Single source of token->dollar math. `costs` maps model id ->
    {in_per_mtok, out_per_mtok}. Unknown model or empty table -> 0.0 (real
    numbers are filled from the pricing page before --live runs)."""
    entry = (costs or {}).get(model)
    if not entry:
        return 0.0
    cin = entry.get("in_per_mtok", 0.0) or 0.0
    cout = entry.get("out_per_mtok", 0.0) or 0.0
    return (tokens_in / 1_000_000) * cin + (tokens_out / 1_000_000) * cout


def _approx_tokens(text: str) -> int:
    """Rough token count for the fake client (~4 chars/token)."""
    return max(1, len(text) // 4)


class LLMClient(ABC):
    @abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        ...


class FakeClient(LLMClient):
    """Deterministic double. `responder(system, user, model) -> str` supplies the
    completion text; token counts are heuristic, cost from the configured table.
    Records every call in `.calls` for test assertions."""

    def __init__(
        self,
        responder: Callable[[str, str, str], str],
        costs: Optional[dict[str, Any]] = None,
    ):
        self.responder = responder
        self.costs = costs or {}
        self.calls: list[dict] = []

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        t0 = time.perf_counter()
        text = self.responder(system, user, model)
        tin = _approx_tokens(system) + _approx_tokens(user)
        tout = _approx_tokens(text)
        self.calls.append({"model": model, "system": system, "user": user, "text": text})
        return LLMResponse(
            text=text,
            tokens_in=tin,
            tokens_out=tout,
            cost_usd=estimate_cost(model, tin, tout, self.costs),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            model=model,
        )


class OpenAICompatClient(LLMClient):
    """openai SDK against an OpenAI-compatible endpoint. Only constructed for --live runs."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        costs: Optional[dict[str, Any]] = None,
    ):
        from openai import OpenAI  # imported lazily so tests never need the SDK configured

        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self.costs = costs or {}

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        t0 = time.perf_counter()
        resp = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content or ""
        usage = resp.usage
        tin = getattr(usage, "prompt_tokens", 0) or 0
        tout = getattr(usage, "completion_tokens", 0) or 0
        return LLMResponse(
            text=text,
            tokens_in=tin,
            tokens_out=tout,
            cost_usd=estimate_cost(model, tin, tout, self.costs),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            model=model,
        )


def build_client(
    cfg: dict[str, Any],
    fake_responder: Optional[Callable[[str, str, str], str]] = None,
) -> LLMClient:
    """Factory. Reads cfg['llm']['provider']. `fake_responder` is required for the
    fake provider (tests/evals supply it) — the factory never invents responses."""
    llm_cfg = cfg.get("llm", {})
    provider = llm_cfg.get("provider", "fake")
    costs = cfg.get("costs", {})

    if provider == "fake":
        if fake_responder is None:
            raise ValueError("provider=fake requires a fake_responder")
        return FakeClient(fake_responder, costs=costs)
    if provider == "openai_compat":
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY not set (required for --live openai_compat)")
        return OpenAICompatClient(llm_cfg["base_url"], api_key, costs=costs)
    raise ValueError(f"unknown or deferred llm provider: {provider!r}")
