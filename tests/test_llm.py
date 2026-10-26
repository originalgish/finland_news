from types import SimpleNamespace

import pytest

from worker.llm import Claude, LLMError, Usage

TOOL = {"name": "record_thing", "input_schema": {"type": "object"}}


def reply(*blocks, input_tokens=10, output_tokens=5, cache_read=0, cache_write=0):
    return SimpleNamespace(
        content=list(blocks),
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


def tool_use(name, payload):
    return SimpleNamespace(type="tool_use", name=name, input=payload)


def text(value):
    return SimpleNamespace(type="text", text=value)


class RecordingClient:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.messages = self

    def create(self, **request):
        self.requests.append(request)
        return self.response


def test_call_tool_forces_the_tool_and_returns_its_input():
    client = RecordingClient(reply(tool_use("record_thing", {"answer": 42})))

    result = Claude(client, "claude-haiku-5-5").call_tool(
        system="be brief", user="question", tool=TOOL, max_tokens=100
    )

    assert result == {"answer": 42}
    request = client.requests[0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["tool_choice"] == {"type": "tool", "name": "record_thing"}
    assert request["tools"] == [TOOL]
    assert request["messages"] == [{"role": "user", "content": "question"}]


def test_the_system_prompt_is_marked_for_caching():
    client = RecordingClient(reply(tool_use("record_thing", {})))

    Claude(client, "m").call_tool(system="static rules", user="x", tool=TOOL, max_tokens=10)

    system = client.requests[0]["system"]
    assert system == [{"type": "text", "text": "static rules", "cache_control": {"type": "ephemeral"}}]


def test_a_reply_without_the_tool_is_an_error():
    client = RecordingClient(reply(text("I would rather not.")))

    with pytest.raises(LLMError, match="did not call record_thing"):
        Claude(client, "m").call_tool(system="s", user="u", tool=TOOL, max_tokens=10)


def test_a_reply_calling_another_tool_is_an_error():
    client = RecordingClient(reply(tool_use("some_other_tool", {})))

    with pytest.raises(LLMError):
        Claude(client, "m").call_tool(system="s", user="u", tool=TOOL, max_tokens=10)


def test_usage_accumulates_across_calls():
    client = RecordingClient(
        reply(tool_use("record_thing", {}), input_tokens=100, output_tokens=20, cache_read=60, cache_write=40)
    )
    llm = Claude(client, "m")

    llm.call_tool(system="s", user="u", tool=TOOL, max_tokens=10)
    llm.call_tool(system="s", user="u", tool=TOOL, max_tokens=10)

    assert llm.usage == Usage(calls=2, input_tokens=200, output_tokens=40, cache_read_tokens=120, cache_write_tokens=80)
    assert "2 calls" in llm.usage.summary()
