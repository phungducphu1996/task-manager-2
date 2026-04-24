from __future__ import annotations

from typing import Any

import httpx

from .config import get_settings

settings = get_settings()


class BotLLMError(RuntimeError):
    pass


def is_bot_llm_configured() -> bool:
    return settings.bot_enabled and bool((settings.openai_api_key or '').strip())


def generate_bot_reply(*, system_prompt: str, user_prompt: str) -> str:
    api_key = (settings.openai_api_key or '').strip()
    if not api_key:
        raise BotLLMError('OPENAI_API_KEY is not configured.')

    base_url = settings.bot_llm_base_url.rstrip('/')
    url = f'{base_url}/chat/completions'
    payload = {
        'model': settings.bot_llm_model,
        'temperature': 0.5,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
    }
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=settings.bot_llm_timeout_seconds)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise BotLLMError(str(exc)) from exc

    data = response.json()
    content = _extract_message_text(data)
    if not content:
        raise BotLLMError('LLM returned an empty response.')
    return content.strip()


def _extract_message_text(data: dict[str, Any]) -> str:
    choices = data.get('choices') or []
    if not choices:
        return ''
    message = choices[0].get('message') or {}
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
