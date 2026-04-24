from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import httpx

from .config import get_settings

settings = get_settings()


class BotLLMError(RuntimeError):
    pass


@dataclass(slots=True)
class BotLLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class BotLLMToolResponse:
    content: str
    tool_calls: list[BotLLMToolCall]
    assistant_message: dict[str, Any]


def is_bot_llm_configured() -> bool:
    return settings.bot_enabled and bool((settings.openai_api_key or '').strip())


def generate_bot_reply(*, system_prompt: str, user_prompt: str) -> str:
    return _request_llm_text(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.5,
    )


def generate_bot_json(*, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    content = _request_llm_text(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.1,
        response_format={'type': 'json_object'},
    )
    normalized = content.strip()
    if normalized.startswith('```'):
        normalized = normalized.strip('`')
        if normalized.lower().startswith('json'):
            normalized = normalized[4:].strip()
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise BotLLMError(f'LLM returned invalid JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise BotLLMError('LLM JSON response must be an object.')
    return parsed


def complete_bot_conversation(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.3,
) -> BotLLMToolResponse:
    data = _request_llm_data(
        messages=messages,
        temperature=temperature,
        tools=tools,
    )
    choices = data.get('choices') or []
    if not choices:
        raise BotLLMError('LLM returned no choices.')
    message = choices[0].get('message') or {}
    tool_calls = _extract_tool_calls(message)
    return BotLLMToolResponse(
        content=_extract_message_text_from_message(message).strip(),
        tool_calls=tool_calls,
        assistant_message=message,
    )


def _request_llm_text(
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    response_format: dict[str, Any] | None = None,
) -> str:
    data = _request_llm_data(
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        temperature=temperature,
        response_format=response_format,
    )
    content = _extract_message_text(data)
    if not content:
        raise BotLLMError('LLM returned an empty response.')
    return content.strip()


def _request_llm_data(
    *,
    messages: list[dict[str, Any]],
    temperature: float,
    response_format: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    api_key = (settings.openai_api_key or '').strip()
    if not api_key:
        raise BotLLMError('OPENAI_API_KEY is not configured.')

    base_url = settings.bot_llm_base_url.rstrip('/')
    url = f'{base_url}/chat/completions'
    payload = {
        'model': settings.bot_llm_model,
        'temperature': temperature,
        'messages': messages,
    }
    if response_format:
        payload['response_format'] = response_format
    if tools:
        payload['tools'] = tools
        payload['tool_choice'] = 'auto'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=settings.bot_llm_timeout_seconds)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise BotLLMError(str(exc)) from exc

    return response.json()


def _extract_message_text(data: dict[str, Any]) -> str:
    choices = data.get('choices') or []
    if not choices:
        return ''
    message = choices[0].get('message') or {}
    return _extract_message_text_from_message(message)


def _extract_message_text_from_message(message: dict[str, Any]) -> str:
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get('text')
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return '\n'.join(parts)
    return ''


def _extract_tool_calls(message: dict[str, Any]) -> list[BotLLMToolCall]:
    result: list[BotLLMToolCall] = []
    for raw_call in message.get('tool_calls') or []:
        if not isinstance(raw_call, dict):
            continue
        if raw_call.get('type') != 'function':
            continue
        function = raw_call.get('function') or {}
        name = str(function.get('name') or '').strip()
        arguments_text = str(function.get('arguments') or '').strip() or '{}'
        if not name:
            continue
        try:
            arguments = json.loads(arguments_text)
        except json.JSONDecodeError:
            raise BotLLMError(f'LLM returned invalid tool arguments for {name}.')
        if not isinstance(arguments, dict):
            raise BotLLMError(f'LLM tool arguments for {name} must be an object.')
        result.append(
            BotLLMToolCall(
                id=str(raw_call.get('id') or ''),
                name=name,
                arguments=arguments,
            )
        )
    return result
