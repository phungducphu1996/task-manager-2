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


@dataclass(slots=True)
class VikunjaTaskSnapshot:
    id: int
    title: str
    status: str
    done: bool
    due_date: str | None
    updated: str | None
    assignee_social_ids: list[str]
    assignee_names: list[str]
    url: str | None

    def as_state(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'title': self.title,
            'status': self.status,
            'done': self.done,
            'due_date': self.due_date,
            'updated': self.updated,
            'assignee_social_ids': self.assignee_social_ids,
            'assignee_names': self.assignee_names,
            'url': self.url,
        }


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

    def list_project_tasks(self, project_id: int, *, page: int = 1, per_page: int = 100) -> list[dict[str, Any]]:
        data = self.request('GET', f'/projects/{project_id}/tasks', params={'page': page, 'per_page': per_page})
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get('data'), list):
            return data['data']
        return []

    def list_all_project_tasks(self, project_id: int, *, per_page: int = 100, max_pages: int = 50) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self.list_project_tasks(project_id, page=page, per_page=per_page)
            tasks.extend(batch)
            if len(batch) < per_page:
                break
        return tasks

    def list_project_views(self, project_id: int) -> list[dict[str, Any]]:
        data = self.request('GET', f'/projects/{project_id}/views')
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get('data'), list):
            return data['data']
        return []

    def list_view_tasks(
        self,
        project_id: int,
        view_id: int,
        *,
        page: int = 1,
        per_page: int = 100,
        filter_query: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {'page': page, 'per_page': per_page}
        if filter_query:
            params['filter'] = filter_query
        data = self.request('GET', f'/projects/{project_id}/views/{view_id}/tasks', params=params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get('data'), list):
            return data['data']
        return []

    def list_all_view_tasks(
        self,
        project_id: int,
        view_id: int,
        *,
        filter_query: str | None = None,
        per_page: int = 100,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self.list_view_tasks(project_id, view_id, page=page, per_page=per_page, filter_query=filter_query)
            tasks.extend(batch)
            if len(batch) < per_page:
                break
        return tasks

    def get_task(self, task_id: int) -> dict[str, Any]:
        return self.request('GET', f'/tasks/{task_id}')

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


def _vikunja_task_url(task_id: int | None) -> str | None:
    if task_id is None:
        return None
    template = (settings.vikunja_task_url_template or '').strip()
    if template:
        return template.format(project_id=settings.vikunja_project_id or '', task_id=task_id)
    base_url = (settings.vikunja_public_url or settings.vikunja_api_url or '').strip().rstrip('/')
    if not base_url:
        return None
    return f'{base_url}/tasks/{task_id}'


def _task_id_from_payload(task: dict[str, Any]) -> int | None:
    try:
        return int(task.get('id'))
    except (TypeError, ValueError):
        return None


def _status_for_bucket_id(bucket_id: Any) -> str | None:
    try:
        normalized_bucket_id = int(bucket_id)
    except (TypeError, ValueError):
        return None

    # Prefer workflow states over the inbox alias when multiple statuses share one bucket.
    for status_value in ('review', 'ready', 'doing', 'done', 'todo', 'inbox'):
        if settings.vikunja_status_bucket_map.get(status_value) == normalized_bucket_id:
            return status_value
    return None


def vikunja_task_status_from_payload(task: dict[str, Any], task_status_map: dict[int, str] | None = None) -> str:
    if bool(task.get('done')):
        return TaskStatus.done.value
    task_id = _task_id_from_payload(task)
    if task_id is not None and task_status_map and task_status_map.get(task_id):
        return task_status_map[task_id]
    bucket_id = task.get('bucket_id')
    bucket_status = _status_for_bucket_id(bucket_id)
    if bucket_status:
        return bucket_status
    return TaskStatus.todo.value


def _kanban_view_id(client: VikunjaClient, project_id: int) -> int | None:
    for view in client.list_project_views(project_id):
        if str(view.get('view_kind') or '').casefold() == 'kanban':
            try:
                return int(view.get('id'))
            except (TypeError, ValueError):
                return None
    return None


def build_vikunja_task_status_map(client: VikunjaClient, project_id: int) -> dict[int, str]:
    """Map task ids to Kanban bucket statuses.

    The project task endpoint can omit bucket_id, which makes open tasks look like
    todo. Reading tasks through the Kanban view gives us the current bucket.
    """
    try:
        view_id = _kanban_view_id(client, project_id)
    except Exception:
        return {}
    if not view_id:
        return {}

    status_by_task_id: dict[int, str] = {}

    try:
        for task in client.list_all_view_tasks(project_id, view_id):
            task_id = _task_id_from_payload(task)
            status_value = _status_for_bucket_id(task.get('bucket_id'))
            if task_id is not None and status_value:
                status_by_task_id[task_id] = status_value
    except Exception:
        pass

    for status_value, bucket_id in settings.vikunja_status_bucket_map.items():
        if status_value == 'inbox':
            continue
        try:
            bucket_tasks = client.list_all_view_tasks(project_id, view_id, filter_query=f'bucket_id = {int(bucket_id)}')
        except Exception:
            continue
        for task in bucket_tasks:
            task_id = _task_id_from_payload(task)
            if task_id is not None:
                status_by_task_id[task_id] = status_value

    return status_by_task_id


def _snapshot_assignees(db: Session, task: dict[str, Any]) -> tuple[list[str], list[str]]:
    assignees = task.get('assignees')
    if not isinstance(assignees, list):
        return [], []

    social_ids: list[str] = []
    names: list[str] = []
    for assignee in assignees:
        if not isinstance(assignee, dict):
            continue
        try:
            vikunja_user_id = int(assignee.get('id'))
        except (TypeError, ValueError):
            vikunja_user_id = None
        if vikunja_user_id is not None:
            mapping = db.scalar(select(VikunjaUserMapping).where(VikunjaUserMapping.vikunja_user_id == vikunja_user_id))
            if mapping and mapping.social_user_id:
                social_ids.append(mapping.social_user_id)
                names.append(mapping.display_name or mapping.username)
                continue
        display = assignee.get('name') or assignee.get('username') or assignee.get('email')
        if display:
            names.append(str(display))
    return social_ids, names


def _snapshot_from_task(
    db: Session,
    task: dict[str, Any],
    *,
    task_status_map: dict[int, str] | None = None,
) -> VikunjaTaskSnapshot | None:
    try:
        task_id = int(task.get('id'))
    except (TypeError, ValueError):
        return None
    assignee_ids, assignee_names = _snapshot_assignees(db, task)
    due_date = task.get('due_date')
    if isinstance(due_date, str) and 'T' in due_date:
        due_date = due_date.split('T', 1)[0]
    updated = task.get('updated') or task.get('updated_at')
    return VikunjaTaskSnapshot(
        id=task_id,
        title=str(task.get('title') or f'Task #{task_id}'),
        status=vikunja_task_status_from_payload(task, task_status_map),
        done=bool(task.get('done')),
        due_date=str(due_date) if due_date else None,
        updated=str(updated) if updated else None,
        assignee_social_ids=assignee_ids,
        assignee_names=assignee_names,
        url=_vikunja_task_url(task_id),
    )


def _local_task_id_for_vikunja(db: Session, vikunja_task_id: int) -> int | None:
    mapping = db.scalar(select(VikunjaTaskMapping).where(VikunjaTaskMapping.vikunja_task_id == vikunja_task_id))
    return mapping.local_task_id if mapping else None


def _snapshot_state_key(vikunja_task_id: int) -> str:
    return f'vikunja_task:{vikunja_task_id}'


def _changed_fields(previous: dict[str, Any], current: VikunjaTaskSnapshot) -> list[str]:
    now_state = current.as_state()
    keys = ['title', 'status', 'done', 'due_date', 'assignee_social_ids']
    return [key for key in keys if previous.get(key) != now_state.get(key)]


def _active_admins(db: Session) -> list[User]:
    return db.scalars(
        select(User)
        .where(User.is_active.is_(True), func.lower(func.coalesce(User.role, '')) == 'admin')
        .order_by(func.lower(func.coalesce(User.full_name, User.username)).asc())
    ).all()


def _user_for_social_id(db: Session, social_user_id: str | None) -> User | None:
    if not social_user_id:
        return None
    return db.get(User, social_user_id)


def _notify_vikunja_task_changes(
    db: Session,
    *,
    previous: dict[str, Any] | None,
    current: VikunjaTaskSnapshot,
    reason: str,
) -> dict[str, int]:
    from .models import NotificationChannel
    from .notifications import NotificationSpec, dispatch_due_notification_events, enqueue_notification_event

    if previous is None:
        return {'created': 0, 'deduped': 0}

    changed = _changed_fields(previous, current)
    if not changed:
        return {'created': 0, 'deduped': 0}

    previous_status = str(previous.get('status') or '')
    local_task_id = _local_task_id_for_vikunja(db, current.id)
    event_suffix = current.updated or _now().isoformat()
    specs: list[NotificationSpec] = []

    def task_line() -> str:
        assignee = ', '.join(current.assignee_names) or 'Unassigned'
        due = current.due_date or 'no due'
        link = f'\n{current.url}' if current.url else ''
        return f'{current.title}\nAssignee: {assignee}\nStatus: {current.status}\nDue: {due}{link}'

    def render_message(*, event_type: str, recipient: User | None, fallback: str, context: dict[str, Any]) -> str:
        from .bot_files import contact_prompt_text_for_user, notification_prompt_text
        from .bot_llm import BotLLMError, generate_bot_reply, is_bot_llm_configured

        if not is_bot_llm_configured():
            return fallback
        payload = {
            'event_type': event_type,
            'recipient': {
                'id': recipient.id,
                'name': recipient.name,
                'username': recipient.username,
                'role': recipient.role,
            }
            if recipient
            else None,
            'task': {
                'id': current.id,
                'title': current.title,
                'status': current.status,
                'previous_status': previous.get('status'),
                'assignee': ', '.join(current.assignee_names) or None,
                'due_date': current.due_date,
                'url': current.url,
            },
            'changed_fields': changed,
            'context': context,
            'recipient_custom_prompt': contact_prompt_text_for_user(recipient) if recipient else '',
        }
        prompt = (
            'Viết một thông báo Zalo tự nhiên cho task event dưới đây.\n'
            'Không dùng chữ "Vikunja". Gọi là task hoặc Task Manager.\n'
            'Trả về duy nhất nội dung tin nhắn, không markdown fence, không JSON.\n\n'
            f'{payload}'
        )
        try:
            message = generate_bot_reply(system_prompt=notification_prompt_text(), user_prompt=prompt).strip()
        except BotLLMError:
            return fallback
        return message[:1200] if message else fallback

    new_assignees = set(current.assignee_social_ids) - set(previous.get('assignee_social_ids') or [])
    if previous.get('id') and new_assignees:
        for social_user_id in new_assignees:
            user = _user_for_social_id(db, social_user_id)
            fallback = f'{user.name if user else "Bạn"} ơi, bạn vừa được assign task mới:\n{task_line()}'
            message = render_message(
                event_type='task_assigned',
                recipient=user,
                fallback=fallback,
                context={'reason': 'assigned', 'task_id': current.id},
            )
            specs.append(
                NotificationSpec(
                    event_key=f'vikunja:task:{current.id}:assigned:{social_user_id}:{event_suffix}',
                    event_type='vikunja_task_assigned',
                    channel=NotificationChannel.user,
                    target_id=user.zalo_user_id if user else None,
                    task_id=local_task_id,
                    user_id=social_user_id,
                    payload={'message': message, 'context': {'source': 'vikunja_realtime', 'reason': 'assigned', 'vikunja_task_id': current.id}},
                )
            )

    if 'status' in changed or 'done' in changed:
        if previous_status == TaskStatus.review.value and current.status == TaskStatus.ready.value:
            for social_user_id in current.assignee_social_ids:
                user = _user_for_social_id(db, social_user_id)
                fallback = f'{user.name if user else "Bạn"} ơi, task đã được duyệt ready rồi nha:\n{task_line()}'
                message = render_message(
                    event_type='task_approved_ready',
                    recipient=user,
                    fallback=fallback,
                    context={'reason': 'review_to_ready', 'task_id': current.id},
                )
                specs.append(
                    NotificationSpec(
                        event_key=f'vikunja:task:{current.id}:review-ready:{social_user_id}:{event_suffix}',
                        event_type='vikunja_task_approved_ready',
                        channel=NotificationChannel.user,
                        target_id=user.zalo_user_id if user else None,
                        task_id=local_task_id,
                        user_id=social_user_id,
                        payload={
                            'message': message,
                            'context': {'source': 'vikunja_realtime', 'reason': 'review_to_ready', 'vikunja_task_id': current.id},
                        },
                    )
                )

        if current.status == TaskStatus.review.value and previous_status != TaskStatus.review.value:
            for admin in _active_admins(db):
                fallback = f'Admin ơi, task vừa vào review:\n{task_line()}'
                message = render_message(
                    event_type='task_submitted_for_review',
                    recipient=admin,
                    fallback=fallback,
                    context={'reason': 'moved_to_review', 'task_id': current.id},
                )
                specs.append(
                    NotificationSpec(
                        event_key=f'vikunja:task:{current.id}:to-review:admin:{admin.id}:{event_suffix}',
                        event_type='vikunja_task_submitted_for_review',
                        channel=NotificationChannel.user,
                        target_id=admin.zalo_user_id,
                        task_id=local_task_id,
                        user_id=admin.id,
                        payload={
                            'message': message,
                            'context': {'source': 'vikunja_realtime', 'reason': 'moved_to_review', 'vikunja_task_id': current.id},
                        },
                    )
                )

        if current.status == TaskStatus.done.value and previous_status != TaskStatus.done.value:
            for admin in _active_admins(db):
                fallback = f'Admin update: task vừa được chuyển done:\n{task_line()}'
                message = render_message(
                    event_type='task_done',
                    recipient=admin,
                    fallback=fallback,
                    context={'reason': 'done', 'task_id': current.id},
                )
                specs.append(
                    NotificationSpec(
                        event_key=f'vikunja:task:{current.id}:done:admin:{admin.id}:{event_suffix}',
                        event_type='vikunja_task_done',
                        channel=NotificationChannel.user,
                        target_id=admin.zalo_user_id,
                        task_id=local_task_id,
                        user_id=admin.id,
                        payload={'message': message, 'context': {'source': 'vikunja_realtime', 'reason': 'done', 'vikunja_task_id': current.id}},
                    )
                )

    passive_fields = [field for field in changed if field in {'title', 'due_date'}]
    if passive_fields and not ({'status', 'done', 'assignee_social_ids'} & set(changed)):
        for social_user_id in current.assignee_social_ids:
            user = _user_for_social_id(db, social_user_id)
            fallback = f'{user.name if user else "Bạn"} ơi, task vừa được cập nhật ({", ".join(passive_fields)}):\n{task_line()}'
            message = render_message(
                event_type='task_updated',
                recipient=user,
                fallback=fallback,
                context={'reason': reason, 'changed_fields': passive_fields, 'task_id': current.id},
            )
            specs.append(
                NotificationSpec(
                    event_key=f'vikunja:task:{current.id}:updated:{social_user_id}:{event_suffix}',
                    event_type='vikunja_task_updated',
                    channel=NotificationChannel.user,
                    target_id=user.zalo_user_id if user else None,
                    task_id=local_task_id,
                    user_id=social_user_id,
                    payload={
                        'message': message,
                        'context': {
                            'source': 'vikunja_realtime',
                            'reason': reason,
                            'changed_fields': passive_fields,
                            'vikunja_task_id': current.id,
                        },
                    },
                )
            )

    created = 0
    deduped = 0
    for spec in specs:
        _, inserted = enqueue_notification_event(db, spec)
        created += int(inserted)
        deduped += int(not inserted)
    if specs:
        db.commit()
        dispatch_due_notification_events(db, limit=min(20, settings.notification_delivery_batch_limit))
    return {'created': created, 'deduped': deduped}


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


def _reconcile_snapshots(
    db: Session,
    *,
    snapshots: list[VikunjaTaskSnapshot],
    reason: str,
) -> dict[str, Any]:
    seeded = 0
    changed = 0
    unchanged = 0
    events_created = 0
    events_deduped = 0

    for snapshot in snapshots:
        state_key = _snapshot_state_key(snapshot.id)
        existing = _state(db, state_key)
        previous = existing.value if existing and isinstance(existing.value, dict) else None
        if previous is None:
            seeded += 1
        else:
            change_names = _changed_fields(previous, snapshot)
            if change_names:
                changed += 1
                stats = _notify_vikunja_task_changes(db, previous=previous, current=snapshot, reason=reason)
                events_created += int(stats.get('created') or 0)
                events_deduped += int(stats.get('deduped') or 0)
            else:
                unchanged += 1
        _set_state(db, state_key, snapshot.as_state())

    _set_state(
        db,
        'vikunja_reconcile',
        {
            'last_run_at': _now().isoformat(),
            'task_count': len(snapshots),
            'reason': reason,
        },
    )
    db.commit()
    return {
        'checked': len(snapshots),
        'seeded': seeded,
        'changed': changed,
        'unchanged': unchanged,
        'events_created': events_created,
        'events_deduped': events_deduped,
    }


def reconcile_vikunja_bridge(db: Session) -> dict[str, Any]:
    require_vikunja_or_503()
    if not settings.vikunja_project_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='VIKUNJA_PROJECT_ID must be configured before reconciling Vikunja tasks.',
    )
    client = get_vikunja_client()
    raw_tasks = client.list_all_project_tasks(settings.vikunja_project_id)
    task_status_map = build_vikunja_task_status_map(client, settings.vikunja_project_id)
    snapshots = [snapshot for task in raw_tasks if (snapshot := _snapshot_from_task(db, task, task_status_map=task_status_map))]
    stats = _reconcile_snapshots(db, snapshots=snapshots, reason='poll_reconcile')
    return {**vikunja_bridge_summary(db), 'reconcile': stats}


def handle_vikunja_webhook(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    _set_state(db, 'last_webhook', {'received_at': _now().isoformat(), 'payload': payload})
    if not settings.vikunja_enabled or not settings.vikunja_project_id:
        db.commit()
        return {'ok': True, 'action': 'recorded', 'reconcile': None}

    try:
        client = get_vikunja_client()
        raw_tasks = client.list_all_project_tasks(settings.vikunja_project_id)
        task_status_map = build_vikunja_task_status_map(client, settings.vikunja_project_id)
        snapshots = [snapshot for task in raw_tasks if (snapshot := _snapshot_from_task(db, task, task_status_map=task_status_map))]
        stats = _reconcile_snapshots(db, snapshots=snapshots, reason='webhook')
    except Exception as exc:
        _set_state(db, 'last_webhook_error', {'received_at': _now().isoformat(), 'error': str(exc), 'payload': payload})
        db.commit()
        return {'ok': False, 'action': 'recorded', 'error': str(exc)}

    return {'ok': True, 'action': 'recorded_and_reconciled', 'reconcile': stats}


def require_vikunja_or_503() -> None:
    if not settings.vikunja_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Vikunja bridge is not configured. Set VIKUNJA_API_URL and VIKUNJA_API_TOKEN.',
        )
