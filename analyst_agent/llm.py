"""LLM-клиенты для Analyst Agent.

* AnthropicClient — Claude через Messages API (tool use). Без внешних SDK,
  только стандартная библиотека; ключ берётся из ANTHROPIC_API_KEY.
* ScriptedLLM     — детерминированный «сценарный» LLM для тестов и офлайн-демо.

Формат сообщений — нативный формат Messages API (role + content blocks).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Union

DEFAULT_MODEL = os.environ.get("ANALYST_MODEL", "claude-opus-5-5")


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"

    def content_blocks(self) -> list[dict]:
        blocks: list[dict] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for c in self.tool_calls:
            blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input})
        return blocks


class LLMClient(Protocol):
    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


class LLMError(RuntimeError):
    pass


class AnthropicClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        base_url: Optional[str] = None,
        max_tokens: int = 8000,
        timeout: float = 300,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise LLMError("Не задан ANTHROPIC_API_KEY")
        self.model = model
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
        }
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise LLMError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:1000]}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"Сетевая ошибка: {e}") from e

        out = LLMResponse(stop_reason=data.get("stop_reason", "end_turn"))
        texts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                texts.append(block["text"])
            elif block.get("type") == "tool_use":
                out.tool_calls.append(ToolCall(block["id"], block["name"], block.get("input") or {}))
        out.text = "\n".join(texts)
        return out


# --------------------------------------------------------------------------
# Сценарный LLM
# --------------------------------------------------------------------------

ScriptItem = Union[LLMResponse, Callable[[list[dict]], LLMResponse]]


class ScriptedLLM:
    """Возвращает заранее заданные ответы по очереди.

    Элемент сценария — LLMResponse или функция(messages) -> LLMResponse
    (функция видит историю, включая результаты инструментов, и может
    сформировать объяснение по фактическому результату Model Checker).
    """

    def __init__(self, script: list[ScriptItem]):
        self.script = list(script)
        self.calls: list[list[dict]] = []

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self.calls.append(json.loads(json.dumps(messages)))
        if not self.script:
            raise LLMError("ScriptedLLM: сценарий исчерпан")
        item = self.script.pop(0)
        return item(messages) if callable(item) else item


def tool_call(name: str, **inputs) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(f"toolu_{uuid.uuid4().hex[:12]}", name, inputs)], stop_reason="tool_use")


def last_tool_result(messages: list[dict]) -> Any:
    """Последний tool_result из истории (распарсенный JSON)."""
    for m in reversed(messages):
        if m["role"] != "user" or not isinstance(m["content"], list):
            continue
        for b in reversed(m["content"]):
            if b.get("type") == "tool_result":
                c = b["content"]
                text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c)
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    return None
