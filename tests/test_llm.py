from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from sentinel.llm import FakeLLM, LLMError, OpenAICompatibleLLM, extract_json


class Out(BaseModel):
    answer: str


def completion(content: str | None, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
        },
    )


def make_llm(responder, **kwargs) -> tuple[OpenAICompatibleLLM, list[dict]]:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return responder(len(seen), seen[-1])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    llm = OpenAICompatibleLLM("http://gpu.internal:8000/v1", "model", http_client=http, **kwargs)
    return llm, seen


def test_request_shape_reasoning_flag_and_schema_enforcement():
    llm, seen = make_llm(lambda n, body: completion('{"answer": "ok"}'))
    assert llm.complete_json(system="s", user="u", schema=Out, reasoning=True) == Out(answer="ok")
    body = seen[0]
    assert body["model"] == "model"
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["properties"]["answer"]
    assert body["temperature"] == 1.0 and body["top_p"] == 1.0
    assert "JSON Schema" in body["messages"][0]["content"]


def test_non_reasoning_is_deterministic_and_disables_thinking():
    llm, seen = make_llm(lambda n, body: completion('{"answer": "ok"}'))
    llm.complete_json(system="s", user="u", schema=Out, reasoning=False)
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen[0]["temperature"] == 0.0


def test_guided_json_mode_uses_extra_body():
    llm, seen = make_llm(
        lambda n, body: completion('{"answer": "ok"}'), structured_mode="guided_json"
    )
    llm.complete_json(system="s", user="u", schema=Out)
    assert "guided_json" in seen[0] and "response_format" not in seen[0]


def test_think_blocks_and_fences_are_stripped():
    text = '<think>hmm {"answer": "wrong"}</think>\n```json\n{"answer": "right"}\n```'
    llm, _ = make_llm(lambda n, body: completion(text))
    assert llm.complete_json(system="s", user="u", schema=Out).answer == "right"


def test_falls_back_to_prompt_mode_when_server_rejects_response_format():
    def responder(n, body):
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "unsupported", "type": "x"}})
        return completion('{"answer": "ok"}')

    llm, seen = make_llm(responder)
    assert llm.complete_json(system="s", user="u", schema=Out).answer == "ok"
    assert "response_format" in seen[0] and "response_format" not in seen[1]
    # The downgrade sticks: later calls go straight to prompt mode.
    llm.complete_json(system="s", user="u", schema=Out)
    assert "response_format" not in seen[2]


def test_invalid_json_is_retried_once_with_feedback():
    replies = ["not json at all", '{"answer": "fixed"}']
    llm, seen = make_llm(lambda n, body: completion(replies[n - 1]))
    assert llm.complete_json(system="s", user="u", schema=Out).answer == "fixed"
    assert len(seen) == 2
    assert "invalid" in seen[1]["messages"][-1]["content"]


def test_persistently_invalid_output_raises():
    llm, seen = make_llm(lambda n, body: completion('{"wrong": 1}'))
    with pytest.raises(LLMError, match="valid JSON"):
        llm.complete_json(system="s", user="u", schema=Out)
    assert len(seen) == 2


def test_truncated_reasoning_gives_actionable_error():
    llm, _ = make_llm(lambda n, body: completion("", finish_reason="length"))
    with pytest.raises(LLMError, match="LLM_MAX_TOKENS"):
        llm.complete_json(system="s", user="u", schema=Out)


def test_server_errors_become_llm_errors():
    llm, _ = make_llm(lambda n, body: httpx.Response(401, json={"error": {"message": "nope"}}))
    with pytest.raises(LLMError, match="request failed"):
        llm.complete_json(system="s", user="u", schema=Out)


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": 1} hope that helps') == {"a": 1}
    # Truncated reasoning must not leak JSON-looking fragments from inside the <think> block.
    with pytest.raises(ValueError):
        extract_json('<think>never closed {"a": 2}')
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_fake_llm_records_calls_and_validates():
    fake = FakeLLM(lambda system, user, schema, reasoning: {"answer": "hi"})
    assert fake.complete_json(system="s", user="u", schema=Out, reasoning=True).answer == "hi"
    assert fake.calls == [{"system": "s", "user": "u", "schema": "Out", "reasoning": True}]
    bad = FakeLLM(lambda *a: {"nope": 1})
    with pytest.raises(LLMError):
        bad.complete_json(system="s", user="u", schema=Out)
