"""LLM access: one OpenAI-compatible client (vLLM in production) plus a scriptable fake.

Everything the engine needs from a model is `complete_json(system, user, schema)`: the reply is
always validated locally against a pydantic schema, whether or not the server enforced it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import httpx
import openai
from pydantic import BaseModel, ValidationError

from .config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LLMError(RuntimeError):
    """The model call failed or returned something unusable."""


class LLM(Protocol):
    model: str

    def complete_json(
        self, *, system: str, user: str, schema: type[T], reasoning: bool = False
    ) -> T: ...


def extract_json(text: str) -> Any:
    """Pull a JSON value out of model text: strips <think> blocks, code fences, stray prose."""
    cleaned = _THINK_RE.sub("", text)
    # An unterminated <think> (truncated reasoning) leaves nothing usable after it.
    if "<think>" in cleaned.lower():
        cleaned = cleaned[: cleaned.lower().index("<think>")]
    cleaned = _FENCE_RE.sub("", cleaned.strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"reply is not valid JSON: {exc.msg}") from exc
    raise ValueError("reply contains no JSON object")


class OpenAICompatibleLLM:
    """Chat-completions client for vLLM (or any OpenAI-compatible server)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "EMPTY",
        *,
        timeout: float = 120.0,
        max_tokens: int = 10_000,
        structured_mode: str = "json_schema",
        temperature_reasoning: float = 1.0,
        http_client: httpx.Client | None = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature_reasoning = temperature_reasoning
        self._mode = structured_mode
        self._client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=2,
            http_client=http_client,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatibleLLM:
        base_url, model = settings.require_llm()
        return cls(
            base_url,
            model,
            settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            structured_mode=settings.llm_structured_mode,
            temperature_reasoning=settings.llm_temperature_reasoning,
        )

    # -- request -----------------------------------------------------------------------

    def _request(self, messages: list[dict], schema_json: dict, reasoning: bool) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            # Nemotron 3 Nano: reasoning on/off is a chat-template kwarg.
            "extra_body": {"chat_template_kwargs": {"enable_thinking": reasoning}},
        }
        if reasoning:
            kwargs["temperature"] = self.temperature_reasoning
            kwargs["top_p"] = 1.0
        else:
            kwargs["temperature"] = 0.0
        if self._mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "sentinel_output", "schema": schema_json, "strict": True},
            }
        elif self._mode == "guided_json":
            kwargs["extra_body"]["guided_json"] = schema_json
        return self._client.chat.completions.create(**kwargs)

    def _chat(self, messages: list[dict], schema_json: dict, reasoning: bool) -> str:
        try:
            try:
                resp = self._request(messages, schema_json, reasoning)
            except openai.BadRequestError as exc:
                if self._mode == "prompt":
                    raise
                log.warning(
                    "server rejected structured output mode %r (%s); falling back to prompt-only "
                    "JSON (output is still validated locally)",
                    self._mode,
                    exc,
                )
                self._mode = "prompt"
                resp = self._request(messages, schema_json, reasoning)
        except openai.OpenAIError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        choice = resp.choices[0]
        content = choice.message.content or ""
        if not content.strip():
            hint = " (output truncated during reasoning; raise LLM_MAX_TOKENS)"
            raise LLMError(
                "LLM returned an empty reply" + (hint if choice.finish_reason == "length" else "")
            )
        return content

    # -- public ------------------------------------------------------------------------

    def complete_json(
        self, *, system: str, user: str, schema: type[T], reasoning: bool = False
    ) -> T:
        schema_json = schema.model_json_schema()
        system_full = (
            f"{system}\n\nReply with ONLY a single JSON object (no prose, no code fences) that "
            f"conforms to this JSON Schema:\n{json.dumps(schema_json)}"
        )
        messages: list[dict] = [
            {"role": "system", "content": system_full},
            {"role": "user", "content": user},
        ]
        last_error: Exception | None = None
        for _attempt in range(2):
            text = self._chat(messages, schema_json, reasoning)
            try:
                return schema.model_validate(extract_json(text))
            except (ValueError, ValidationError) as exc:
                last_error = exc
                messages += [
                    {"role": "assistant", "content": text[:4000]},
                    {
                        "role": "user",
                        "content": f"That reply was invalid: {exc}\nReturn only the corrected "
                        "JSON object.",
                    },
                ]
        raise LLMError(f"model did not return valid JSON for {schema.__name__}: {last_error}")


class FakeLLM:
    """Deterministic stand-in for tests: `handler(system, user, schema, reasoning)` -> dict/model.

    Every call is recorded in `calls` so tests can assert on what the model was sent.
    """

    def __init__(
        self,
        handler: Callable[[str, str, type[BaseModel], bool], dict | BaseModel],
        model: str = "fake-model",
    ):
        self.model = model
        self._handler = handler
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self, *, system: str, user: str, schema: type[T], reasoning: bool = False
    ) -> T:
        self.calls.append(
            {"system": system, "user": user, "schema": schema.__name__, "reasoning": reasoning}
        )
        out = self._handler(system, user, schema, reasoning)
        if isinstance(out, BaseModel):
            return schema.model_validate(out.model_dump())
        try:
            return schema.model_validate(out)
        except ValidationError as exc:
            raise LLMError(f"fake model produced invalid {schema.__name__}: {exc}") from exc
