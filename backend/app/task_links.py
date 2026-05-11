from __future__ import annotations

from .config import get_settings

settings = get_settings()


def legacy_task_url(task_id: int | None) -> str | None:
    if task_id is None:
        return None
    base_url = (settings.task_public_base_url or '').strip().rstrip('/')
    if not base_url:
        return None
    return f'{base_url}/tasks/{task_id}'


def vikunja_task_url(task_id: int | None) -> str | None:
    if task_id is None:
        return None
    template = (settings.vikunja_task_url_template or '').strip()
    if template:
        return template.format(project_id=settings.vikunja_project_id or '', task_id=task_id)
    base_url = (settings.vikunja_public_url or settings.vikunja_api_url or '').strip().rstrip('/')
    if not base_url:
        return None
    return f'{base_url}/tasks/{task_id}'


def ensure_task_link(message: str, url: str | None, *, label: str = 'Link task') -> str:
    clean_message = (message or '').strip()
    clean_url = (url or '').strip()
    if not clean_url:
        return clean_message
    if clean_url in clean_message:
        return clean_message
    link_line = f'{label}: {clean_url}'
    if not clean_message:
        return link_line
    return f'{clean_message}\n{link_line}'
