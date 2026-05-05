from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from .config import get_settings
from .models import (
    Shop,
    Task,
    TaskAttachment,
    TaskComment,
    TaskPriority,
    TaskStatus,
    TaskType,
    User,
    VikunjaBridgeState,
    VikunjaTaskMapping,
    VikunjaUserMapping,
)

settings = get_settings()

STATUS_BUCKET_TITLES: dict[str, str] = {
    'inbox': 'Inbox',
    TaskStatus.todo.value: 'Todo',
    TaskStatus.doing.value: 'Doing',
    TaskStatus.review.value: 'Review',
    TaskStatus.ready.value: 'Ready',
    TaskStatus.done.value: 'Done/Logbook',
}

PRIORITY_MAP = {
    TaskPriority.low: 2,
    TaskPriority.medium: 3,
    TaskPriority.high: 4,
    TaskPriority.urgent: 5,
}


@dataclass(slots=True)
class VikunjaRequestResult:
    ok: bool
    status_code: int | None = None
    data: Any = None
    error: str | None = None


class VikunjaConfigError(RuntimeError):
    pass


class VikunjaClient:
    def __init__(self, *, api_url: str, api_token: str, timeout: float = 20.0) -> None:
        raw = api_url.strip().rstrip('/')
        if raw.endswith('/api/v1'):
            self.base_url = raw
        else:
            self.base_url = f'{raw}/api/v1'
        self.api_token = api_token.strip()
        self.timeout = timeout

    def request(self, method: str, path: str, *, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> Any:
        headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        url = f'{self.base_url}/{path.lstrip("/")}'
        response = httpx.request(method, url, headers=headers, json=json, params=params, timeout=self.timeout)
        if response.status_code >= 400:
            raise RuntimeError(f'Vikunja {method} {path} failed with {response.status_code}: {response.text}')
        if not response.content:
            return None
        return response.json()

    def find_users(self, search: str) -> list[dict[str, Any]]:
        data = self.request('GET', '/users', params={'s': search})
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get('data'), list):
            return data['data']
        return []

    def create_project(self, title: str) -> dict[str, Any]:
        return self.request('PUT', '/projects', json={'title': title})

    def create_task(self, project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request('PUT', f'/projects/{project_id}/tasks', json=payload)

    def update_task(self, task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request('POST', f'/tasks/{task_id}', json=payload)

    def create_task_comment(self, task_id: int, comment: str) -> dict[str, Any]:
        return self.request('PUT', f'/tasks/{task_id}/comments', json={'comment': comment})


def vikunja_configured() -> bool:
    return settings.vikunja_enabled


def get_vikunja_client() -> VikunjaClient:
    if not settings.vikunja_enabled:
        raise VikunjaConfigError('VIKUNJA_API_URL and VIKUNJA_API_TOKEN must be configured.')
    return VikunjaClient(
        api_url=settings.vikunja_api_url or '',
        api_token=settings.vikunja_api_token or '',
        timeout=settings.notification_http_timeout_seconds,
    )


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.app_timezone))


def _state(db: Session, key: str) -> VikunjaBridgeState | None:
    return db.get(VikunjaBridgeState, key)


def _set_state(db: Session, key: str, value: dict[str, Any]) -> None:
    state = _state(db, key)
    if not state:
        state = VikunjaBridgeState(key=key, value=value)
    else:
        state.value = value
    db.add(state)


def ensure_vikunja_project(db: Session, client: VikunjaClient) -> int:
    if settings.vikunja_project_id:
        return settings.vikunja_project_id
    existing = _state(db, 'project')
    if existing and isinstance(existing.value, dict) and existing.value.get('id'):
        return int(existing.value['id'])
    project = client.create_project(settings.vikunja_project_title)
    project_id = int(project.get('id'))
    _set_state(db, 'project', {'id': project_id, 'title': project.get('title') or settings.vikunja_project_title})
    db.commit()
    return project_id


def _extract_vikunja_user_id(candidate: dict[str, Any]) -> int | None:
    raw = candidate.get('id')
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def sync_vikunja_users(db: Session, client: VikunjaClient | None = None) -> dict[str, Any]:
    client = client or get_vikunja_client()
    users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(func.lower(User.username).asc())).all()
    created = 0
    matched = 0
    missing = 0
    results: list[dict[str, Any]] = []

    for user in users:
        mapping = db.scalar(select(VikunjaUserMapping).where(VikunjaUserMapping.social_user_id == user.id))
        if not mapping:
            mapping = VikunjaUserMapping(
                social_user_id=user.id,
                username=user.username,
                display_name=user.name,
                zalo_user_id=user.zalo_user_id,
                role=user.role,
                sync_status='pending',
                metadata_json={},
            )
            created += 1
        mapping.username = user.username
        mapping.display_name = user.name
        mapping.zalo_user_id = user.zalo_user_id
        mapping.role = user.role

        try:
            candidates = client.find_users(user.username)
            exact = next((item for item in candidates if str(item.get('username') or '').casefold() == user.username.casefold()), None)
            candidate = exact or (candidates[0] if candidates else None)
            if candidate:
                mapping.vikunja_user_id = _extract_vikunja_user_id(candidate)
                mapping.sync_status = 'matched' if mapping.vikunja_user_id else 'missing'
                mapping.sync_error = None if mapping.vikunja_user_id else 'Matched user payload has no id.'
                mapping.metadata_json = {'vikunja_user': candidate}
                if mapping.vikunja_user_id:
                    matched += 1
                else:
                    missing += 1
            else:
                mapping.vikunja_user_id = None
                mapping.sync_status = 'missing'
                mapping.sync_error = 'Create the matching local Vikunja user first.'
                mapping.metadata_json = {'searched': user.username}
                missing += 1
        except Exception as exc:
            mapping.sync_status = 'failed'
            mapping.sync_error = str(exc)
            mapping.metadata_json = {'searched': user.username}
            missing += 1
        db.add(mapping)
        results.append(
            {
                'social_user_id': user.id,
                'username': user.username,
                'name': user.name,
                'vikunja_user_id': mapping.vikunja_user_id,
                'status': mapping.sync_status,
                'error': mapping.sync_error,
            }
        )

    db.commit()
    return {'users_checked': len(users), 'mappings_created': created, 'matched': matched, 'missing': missing, 'results': results}


def _bucket_id_for_task(task: Task) -> int | None:
    return settings.vikunja_status_bucket_map.get(task.status.value)


def _task_due_datetime(task: Task) -> str | None:
    if not task.due_date:
        return None
    value = datetime.combine(task.due_date, time(hour=23, minute=59), tzinfo=ZoneInfo(settings.app_timezone))
    return value.isoformat()


def _task_description(task: Task) -> str:
    chunks: list[str] = []
    if task.description:
        chunks.append(task.description.strip())
    if task.notes:
        chunks.append(f'Notes:\n{task.notes.strip()}')

    metadata = [
        f'Legacy task id: {task.id}',
        f'Legacy status: {task.status.value}',
        f'Priority: {task.priority.value}',
    ]
    if task.shop:
        metadata.append(f'Shop: {task.shop.name}')
    if task.task_type:
        metadata.append(f'Type: {task.task_type.name}')
    if task.parent_task_id:
        metadata.append(f'Converted from legacy task: {task.parent_task_id}')
    chunks.append('Migration metadata:\n' + '\n'.join(f'- {item}' for item in metadata))

    if task.subtasks:
        chunks.append('Checklist:\n' + '\n'.join(f'- [{"x" if item.is_done else " "}] {item.content}' for item in task.subtasks))

    links: list[str] = []
    for attachment in task.attachments:
        if attachment.mime_type == 'text/uri-list':
            links.append(f'- [{attachment.name}]({attachment.data_url})')
        elif attachment.storage_path or attachment.data_url.startswith('http'):
            links.append(f'- {attachment.name}: {attachment.data_url}')
        else:
            links.append(f'- {attachment.name} ({attachment.mime_type}, {attachment.size_bytes} bytes): preserved in legacy app')
    if links:
        chunks.append('Attachments:\n' + '\n'.join(links))
    return '\n\n---\n\n'.join(chunk for chunk in chunks if chunk.strip())


def _assignee_payload(db: Session, task: Task) -> list[dict[str, int]]:
    if not task.assigned_to:
        return []
    mapping = db.scalar(select(VikunjaUserMapping).where(VikunjaUserMapping.social_user_id == str(task.assigned_to)))
    if mapping and mapping.vikunja_user_id:
        return [{'id': int(mapping.vikunja_user_id)}]
    return []


def _task_payload(db: Session, task: Task, project_id: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'title': task.title,
        'description': _task_description(task),
        'project_id': project_id,
        'done': task.status == TaskStatus.done,
        'priority': PRIORITY_MAP.get(task.priority, 3),
    }
    due = _task_due_datetime(task)
    if due:
        payload['due_date'] = due
    assignees = _assignee_payload(db, task)
    if assignees:
        payload['assignees'] = assignees
    bucket_id = _bucket_id_for_task(task)
    if bucket_id:
        payload['bucket_id'] = bucket_id
    return payload


def _extract_task_id(response: dict[str, Any]) -> int:
    task_id = response.get('id')
    if task_id is None and isinstance(response.get('task'), dict):
        task_id = response['task'].get('id')
    return int(task_id)


def _migrated_comment(comment: TaskComment) -> str:
    author = comment.author.name if comment.author else 'Unknown'
    return f'[Legacy comment from {author} at {comment.created_at.isoformat()}]\n{comment.content}'


def migrate_tasks_to_vikunja(
    db: Session,
    *,
    client: VikunjaClient | None = None,
    force: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    client = client or get_vikunja_client()
    project_id = ensure_vikunja_project(db, client)
    stmt = (
        select(Task)
        .options(
            joinedload(Task.assignee),
            joinedload(Task.creator),
            joinedload(Task.shop),
            joinedload(Task.task_type),
            joinedload(Task.subtasks),
            joinedload(Task.comments).joinedload(TaskComment.author),
            joinedload(Task.attachments),
        )
        .order_by(Task.created_at.asc(), Task.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    tasks = db.scalars(stmt).unique().all()

    migrated = 0
    skipped = 0
    failed = 0
    results: list[dict[str, Any]] = []
    for task in tasks:
        mapping = db.scalar(select(VikunjaTaskMapping).where(VikunjaTaskMapping.local_task_id == task.id))
        if mapping and mapping.vikunja_task_id and not force:
            skipped += 1
            results.append({'local_task_id': task.id, 'status': 'already_migrated', 'vikunja_task_id': mapping.vikunja_task_id})
            continue
        if not mapping:
            mapping = VikunjaTaskMapping(
                local_task_id=task.id,
                vikunja_project_id=project_id,
                source_status=task.status.value,
                sync_status='pending',
                metadata_json={},
            )
        payload = _task_payload(db, task, project_id)
        if dry_run:
            skipped += 1
            results.append({'local_task_id': task.id, 'status': 'dry_run', 'payload': payload})
            continue
        try:
            try:
                response = client.create_task(project_id, payload)
            except Exception as exc:
                if 'assignee' not in str(exc).lower() and 'bucket' not in str(exc).lower():
                    raise
                retry_payload = dict(payload)
                retry_payload.pop('assignees', None)
                retry_payload.pop('bucket_id', None)
                response = client.create_task(project_id, retry_payload)
                mapping.metadata_json = {'retry_without_optional_fields': True, 'original_error': str(exc)}
            vikunja_task_id = _extract_task_id(response)
            for comment in task.comments:
                try:
                    client.create_task_comment(vikunja_task_id, _migrated_comment(comment))
                except Exception as exc:
                    mapping.metadata_json = {**(mapping.metadata_json or {}), 'comment_migration_warning': str(exc)}
            mapping.vikunja_task_id = vikunja_task_id
            mapping.vikunja_project_id = project_id
            mapping.vikunja_bucket_id = payload.get('bucket_id')
            mapping.source_status = task.status.value
            mapping.sync_status = 'migrated'
            mapping.sync_error = None
            mapping.migrated_at = _now()
            mapping.last_synced_at = _now()
            db.add(mapping)
            migrated += 1
            results.append({'local_task_id': task.id, 'status': 'migrated', 'vikunja_task_id': vikunja_task_id})
        except Exception as exc:
            mapping.sync_status = 'failed'
            mapping.sync_error = str(exc)
            mapping.source_status = task.status.value
            mapping.vikunja_project_id = project_id
            db.add(mapping)
            failed += 1
            results.append({'local_task_id': task.id, 'status': 'failed', 'error': str(exc)})
    db.commit()
    return {'project_id': project_id, 'checked': len(tasks), 'migrated': migrated, 'skipped': skipped, 'failed': failed, 'results': results}


def vikunja_bridge_summary(db: Session) -> dict[str, Any]:
    user_total = int(db.scalar(select(func.count(VikunjaUserMapping.id))) or 0)
    user_matched = int(db.scalar(select(func.count(VikunjaUserMapping.id)).where(VikunjaUserMapping.vikunja_user_id.is_not(None))) or 0)
    task_total = int(db.scalar(select(func.count(VikunjaTaskMapping.id))) or 0)
    task_migrated = int(db.scalar(select(func.count(VikunjaTaskMapping.id)).where(VikunjaTaskMapping.vikunja_task_id.is_not(None))) or 0)
    failed_tasks = int(db.scalar(select(func.count(VikunjaTaskMapping.id)).where(VikunjaTaskMapping.sync_status == 'failed')) or 0)
    state = db.scalars(select(VikunjaBridgeState)).all()
    return {
        'configured': settings.vikunja_enabled,
        'project_id': settings.vikunja_project_id or (_state(db, 'project').value.get('id') if _state(db, 'project') else None),
        'users': {'mappings': user_total, 'matched': user_matched, 'missing': user_total - user_matched},
        'tasks': {'mappings': task_total, 'migrated': task_migrated, 'failed': failed_tasks},
        'state': {item.key: item.value for item in state},
        'bucket_map': settings.vikunja_status_bucket_map,
    }


def reconcile_vikunja_bridge(db: Session) -> dict[str, Any]:
    # V1 reconcile is intentionally conservative: it reports bridge state without mutating Vikunja.
    return vikunja_bridge_summary(db)


def handle_vikunja_webhook(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    _set_state(db, 'last_webhook', {'received_at': _now().isoformat(), 'payload': payload})
    db.commit()
    return {'ok': True, 'action': 'recorded'}


def require_vikunja_or_503() -> None:
    if not settings.vikunja_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Vikunja bridge is not configured. Set VIKUNJA_API_URL and VIKUNJA_API_TOKEN.',
        )
