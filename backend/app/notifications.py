from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import inspect
import json
from logging import getLogger
import hmac
import random
import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .bot_files import contact_prompt_text_for_user, notification_event_prompt_text, notification_prompt_text
from .bot_llm import BotLLMError, generate_bot_reply, is_bot_llm_configured
from .config import get_settings
from .models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationEvent,
    NotificationStatus,
    Task,
    TaskStatus,
    User,
    VikunjaUserMapping,
)
from .task_links import ensure_task_link, legacy_task_url, vikunja_task_url
from .vikunja import build_vikunja_task_status_map, get_vikunja_client, vikunja_task_status_from_payload

logger = getLogger(__name__)
settings = get_settings()

MORNING_GROUP_TEMPLATES = [
    "Xin chào team, hôm nay mỗi người có vài nhiệm vụ nhỏ xinh nè:\n\n{{#each user_summaries}}• {{name}}: {{task_count}} task đang chờ bạn xử lý 💪\n{{/each}}\nCố lên nha, xong sớm nghỉ sớm 😆",
    "Hello mọi người, điểm danh task hôm nay nào:\n\n{{#each user_summaries}}• {{name}} có {{task_count}} nhiệm vụ đang chờ xử lý 🔥\n{{/each}}\nLet’s gooo, clear hết là win ngày hôm nay nha!",
    "Chào buổi sáng mọi người 🌤️\nNhắc nhẹ task hôm nay nha:\n\n{{#each user_summaries}}• {{name}} có {{task_count}} task cần hoàn thành ✨\n{{/each}}\nLàm từ từ nhưng nhớ làm nhaaa 😆",
]

MORNING_ADMIN_TEMPLATES = [
    "Hello admin 👑\n\nHiện tại có {{pending_count}} task đang chờ bạn duyệt:\n{{#each pending_tasks}}• {{task_title}} — từ {{assignee}}\n{{/each}}\n👉 Rảnh check giúp team để họ triển khai tiếp nha 🔥",
]

MORNING_USER_TEMPLATES = [
    "Chào {{name}} 👋\n\nHôm nay bạn có {{total_tasks}} task cần xử lý nè:\n\n🧩 Việc cần làm hôm nay:\n{{#each today_tasks}}• {{task_title}}\n  → {{task_description}}\n{{/each}}\n✅ Task đã được admin duyệt:\n{{#each approved_tasks}}• {{task_title}}\n{{/each}}\n{{approved_footer}}\n\nChúc bạn hôm nay làm việc mượt mà ✨",
]

EVENING_GROUP_TEMPLATES = [
    "📰 Bản tin 6PM đây bà con ơi:\n\nHôm nay team mình:\n• Clear được {{total_done}} task 🧹\n• Còn {{total_pending}} task đang “mai tính” 😳\n\nPhần còn lại… hẹn ngày mai xử lý tiếp nha 😆\nTeam mình vẫn ổn áp 💛",
    "⏰ 6PM update cho team nè:\n\n📊 Tổng kết hôm nay:\n• {{total_done}} task đã hoàn thành ✅\n• {{total_pending}} task còn lại ⏳\n\nAi còn task thì cố gắng xử lý nốt hoặc note lại cho ngày mai nha 😆\nGood job cả team 💪",
]


@dataclass(slots=True)
class NotificationSpec:
    event_key: str
    event_type: str
    channel: NotificationChannel
    target_id: str | None
    payload: dict[str, Any]
    task_id: int | None = None
    user_id: str | None = None


@dataclass(slots=True)
class DailyTaskSnapshot:
    id: int
    title: str
    status: str
    done: bool
    due_date: date | None
    updated_at: datetime | None
    assignee_user_ids: list[str]
    assignee_names: list[str]
    url: str | None
    description: str | None = None


def now_local() -> datetime:
    return datetime.now(ZoneInfo(settings.notify_timezone))


def is_internal_token_valid(token: str | None) -> bool:
    expected = settings.notify_internal_token
    if not expected:
        return False
    if not token:
        return False
    return hmac.compare_digest(token, expected)


def _resolve_path(context: dict[str, Any], path: str) -> Any:
    value: Any = context
    for part in path.split('.'):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = getattr(value, part, None)
        if value is None:
            return None
    return value


def render_template(template: str, context: dict[str, Any]) -> str:
    each_pattern = re.compile(r'{{#each\s+([\w.]+)\s*}}(.*?){{/each}}', re.DOTALL)
    var_pattern = re.compile(r'{{\s*([\w.]+)\s*}}')

    def _render_each(match: re.Match[str]) -> str:
        key = match.group(1)
        block = match.group(2)
        value = _resolve_path(context, key)
        if not isinstance(value, list):
            return ''

        rendered: list[str] = []
        for item in value:
            if isinstance(item, dict):
                merged = {**context, **item}
            else:
                merged = {**context, 'this': item}
            rendered.append(render_template(block, merged))
        return ''.join(rendered)

    def _replace_var(match: re.Match[str]) -> str:
        key = match.group(1)
        value = _resolve_path(context, key)
        if value is None:
            return ''
        return str(value)

    rendered = template
    while True:
        next_rendered = each_pattern.sub(_render_each, rendered)
        if next_rendered == rendered:
            break
        rendered = next_rendered

    rendered = var_pattern.sub(_replace_var, rendered)
    return rendered


def _choose_template(templates: list[str]) -> str:
    return random.choice(templates)


def _build_worker_request(event: NotificationEvent) -> dict[str, Any]:
    payload = dict(event.payload or {})
    message = str(payload.get('message', '')).strip()
    context = payload.get('context')
    if not isinstance(context, dict):
        context = {}

    context.setdefault('event_id', event.id)
    context.setdefault('event_type', event.event_type)
    context.setdefault('task_id', event.task_id)
    context.setdefault('user_id', event.user_id)

    return {
        'channel': event.channel.value,
        'target_id': event.target_id,
        'message': message,
        'context': context,
    }


def _resolve_worker_send_url() -> str | None:
    raw_url = (settings.zalo_worker_url or '').strip()
    if not raw_url:
        return None

    parsed = urlsplit(raw_url)
    if parsed.scheme and parsed.netloc:
        path = parsed.path or '/'
        if path in {'', '/'}:
            path = '/api/send-text'
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
    return raw_url


def _to_worker_send_text_payload(request_payload: dict[str, Any]) -> dict[str, Any]:
    channel = str(request_payload.get('channel') or '').strip().lower()
    target_id = str(request_payload.get('target_id') or '').strip()
    message = str(request_payload.get('message') or '').strip()
    context = request_payload.get('context')
    if not isinstance(context, dict):
        context = {}

    target: dict[str, Any] = {}
    if channel == NotificationChannel.group.value:
        target['group_chat_id'] = target_id
    elif channel == NotificationChannel.user.value:
        target['user_zalo_id'] = target_id

    event_type = str(context.get('event_type') or context.get('job') or '').strip()
    payload: dict[str, Any] = {
        'text': message,
        'target': target,
    }
    if event_type:
        payload['event_type'] = event_type
    return payload


def _delivery_config(db: Session | None = None) -> dict[str, Any]:
    if db is None:
        return {}
    from .gmail_monitor import gmail_zalo_config

    return gmail_zalo_config(db)


def _resolve_worker_send_url_from_config(config: dict[str, Any] | None = None) -> str | None:
    raw_url = str((config or {}).get('zalo_worker_url') or settings.zalo_worker_url or '').strip()
    if not raw_url:
        return None

    parsed = urlsplit(raw_url)
    if parsed.scheme and parsed.netloc:
        path = parsed.path or '/'
        if path in {'', '/'}:
            path = '/api/send-text'
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
    return raw_url


def _call_worker(
    request_payload: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> tuple[bool, int | None, str | None, str | None]:
    worker_url = _resolve_worker_send_url_from_config(config)
    if not worker_url:
        return False, None, None, 'ZALO_WORKER_URL is not configured.'

    headers = {'Content-Type': 'application/json'}
    worker_token = str((config or {}).get('zalo_worker_token') or settings.zalo_worker_token or '').strip()
    shared_secret = str((config or {}).get('zalo_shared_secret') or settings.zalo_shared_secret or '').strip()
    if worker_token:
        headers['Authorization'] = f'Bearer {worker_token}'
    if shared_secret:
        headers['X-Internal-Secret'] = shared_secret

    worker_payload = _to_worker_send_text_payload(request_payload)

    try:
        response = httpx.post(
            worker_url,
            json=worker_payload,
            headers=headers,
            timeout=settings.notification_http_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        return False, None, None, str(exc)

    body = response.text
    if response.is_success:
        return True, response.status_code, body, None
    return False, response.status_code, body, f'Worker returned status {response.status_code}'


def send_zalo_text(
    *,
    channel: NotificationChannel,
    target_id: str | None,
    message: str,
    context: dict[str, Any] | None = None,
) -> tuple[bool, int | None, str | None, str | None]:
    return _call_worker(
        {
            'channel': channel.value,
            'target_id': target_id,
            'message': message,
            'context': context or {},
        }
    )


def _retry_delay_for_attempt(attempt_count: int) -> int:
    delays = settings.notification_retry_delays
    if not delays:
        return 0
    index = max(0, min(attempt_count - 1, len(delays) - 1))
    return delays[index]


def enqueue_notification_event(db: Session, spec: NotificationSpec) -> tuple[NotificationEvent, bool]:
    existing = db.scalar(select(NotificationEvent).where(NotificationEvent.event_key == spec.event_key))
    if existing:
        return existing, False

    status = NotificationStatus.pending
    last_error: str | None = None
    if not spec.target_id:
        status = NotificationStatus.skipped
        last_error = 'Missing target_id for notification delivery.'

    event = NotificationEvent(
        event_key=spec.event_key,
        event_type=spec.event_type,
        channel=spec.channel,
        target_id=spec.target_id,
        task_id=spec.task_id,
        user_id=spec.user_id,
        payload=spec.payload,
        status=status,
        last_error=last_error,
    )

    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(NotificationEvent).where(NotificationEvent.event_key == spec.event_key))
        if existing:
            return existing, False
        raise

    if status == NotificationStatus.skipped:
        logger.warning(
            'Notification skipped for event_key=%s: missing target (event_type=%s).',
            spec.event_key,
            spec.event_type,
        )

    return event, True


def _deliver_event_once(db: Session, event: NotificationEvent, *, now: datetime) -> NotificationDelivery:
    request_payload = _build_worker_request(event)
    config = _delivery_config(db)
    next_attempt = event.attempt_count + 1

    delivery = NotificationDelivery(
        event_id=event.id,
        attempt=next_attempt,
        request_payload=request_payload,
    )

    worker_params = inspect.signature(_call_worker).parameters
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in worker_params.values())
    if 'config' in worker_params or accepts_kwargs:
        ok, response_status, response_body, error_message = _call_worker(request_payload, config=config)
    else:
        ok, response_status, response_body, error_message = _call_worker(request_payload)

    delivery.response_status = response_status
    delivery.response_body = response_body
    delivery.error = error_message

    event.attempt_count = next_attempt
    if ok:
        event.status = NotificationStatus.sent
        event.last_error = None
        event.delivered_at = now
        event.next_retry_at = None
    else:
        max_retries = max(0, settings.notification_max_retries)
        if next_attempt <= max_retries:
            event.status = NotificationStatus.pending
            delay = _retry_delay_for_attempt(next_attempt)
            event.next_retry_at = now + timedelta(seconds=delay)
        else:
            event.status = NotificationStatus.failed
            event.next_retry_at = None
        event.last_error = (error_message or response_body or 'Unknown delivery error.')[:2000]

    db.add(delivery)
    return delivery


def dispatch_due_notification_events(db: Session, *, limit: int | None = None) -> dict[str, int]:
    now = now_local()
    batch_limit = limit or settings.notification_delivery_batch_limit

    stmt = (
        select(NotificationEvent)
        .where(NotificationEvent.status == NotificationStatus.pending)
        .where(or_(NotificationEvent.next_retry_at.is_(None), NotificationEvent.next_retry_at <= now))
        .order_by(NotificationEvent.created_at.asc(), NotificationEvent.id.asc())
        .limit(batch_limit)
    )
    events = db.scalars(stmt).all()

    stats = {
        'processed': 0,
        'sent': 0,
        'pending': 0,
        'failed': 0,
    }

    for event in events:
        stats['processed'] += 1
        _deliver_event_once(db, event, now=now)
        if event.status == NotificationStatus.sent:
            stats['sent'] += 1
        elif event.status == NotificationStatus.failed:
            stats['failed'] += 1
        else:
            stats['pending'] += 1

    if events:
        db.commit()

    return stats


def _task_title(task: Task) -> str:
    return task.title.strip() if task.title else f'Task #{task.id}'


def _task_url(task_id: int | None) -> str | None:
    return legacy_task_url(task_id)


def _vikunja_task_url(task_id: int | None) -> str | None:
    return vikunja_task_url(task_id)


def _parse_vikunja_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _parse_vikunja_date(value: Any) -> date | None:
    parsed = _parse_vikunja_datetime(value)
    return parsed.date() if parsed else None


def _vikunja_status_from_task(task: dict[str, Any], task_status_map: dict[int, str] | None = None) -> str:
    return vikunja_task_status_from_payload(task, task_status_map)


def _vikunja_assignees(db: Session, task: dict[str, Any]) -> tuple[list[str], list[str]]:
    assignees = task.get('assignees')
    if not isinstance(assignees, list):
        return [], []

    user_ids: list[str] = []
    names: list[str] = []
    for assignee in assignees:
        if not isinstance(assignee, dict):
            continue
        vikunja_user_id = assignee.get('id')
        mapping = None
        try:
            mapping = db.scalar(
                select(VikunjaUserMapping).where(VikunjaUserMapping.vikunja_user_id == int(vikunja_user_id))
            )
        except (TypeError, ValueError):
            mapping = None
        if mapping and mapping.social_user_id:
            user_ids.append(mapping.social_user_id)
        display = assignee.get('name') or assignee.get('username') or assignee.get('email')
        if display:
            names.append(str(display))
    return user_ids, names


def _fetch_vikunja_daily_snapshots(db: Session) -> list[DailyTaskSnapshot] | None:
    if not settings.vikunja_enabled or not settings.vikunja_project_id:
        return None

    try:
        client = get_vikunja_client()
        tasks = client.list_all_project_tasks(settings.vikunja_project_id)
        task_status_map = build_vikunja_task_status_map(client, settings.vikunja_project_id)
    except Exception as exc:
        logger.warning('Falling back to legacy daily notifications because Vikunja task fetch failed: %s', exc)
        return None

    snapshots: list[DailyTaskSnapshot] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        try:
            task_id = int(task.get('id'))
        except (TypeError, ValueError):
            continue
        assignee_ids, assignee_names = _vikunja_assignees(db, task)
        snapshots.append(
            DailyTaskSnapshot(
                id=task_id,
                title=str(task.get('title') or f'Task #{task_id}'),
                status=_vikunja_status_from_task(task, task_status_map),
                done=bool(task.get('done')),
                due_date=_parse_vikunja_date(task.get('due_date')),
                updated_at=_parse_vikunja_datetime(task.get('updated')),
                assignee_user_ids=assignee_ids,
                assignee_names=assignee_names,
                url=_vikunja_task_url(task_id),
                description=str(task.get('description') or '').strip() or None,
            )
        )
    return snapshots


def _task_notification_payload(
    *,
    event_type: str,
    task: Task,
    recipient: User | None,
    actor: User | None,
    previous_status: TaskStatus | None = None,
    changed_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        'event_type': event_type,
        'recipient': {
            'id': recipient.id,
            'name': recipient.name,
            'username': recipient.username,
            'role': recipient.role,
        }
        if recipient
        else None,
        'actor': {
            'id': actor.id,
            'name': actor.name,
            'username': actor.username,
            'role': actor.role,
        }
        if actor
        else None,
        'task': {
            'id': task.id,
            'title': _task_title(task),
            'status': task.status.value,
            'previous_status': previous_status.value if previous_status else None,
            'assignee': task.assignee.name if task.assignee else None,
            'assigned_to': task.assigned_to,
            'shop': task.shop.name if task.shop else None,
            'type': task.task_type.name if task.task_type else None,
            'due_date': task.due_date.isoformat() if task.due_date else None,
            'priority': task.priority.value if task.priority else None,
            'url': _task_url(task.id),
        },
        'changed_fields': changed_fields or [],
        'recipient_custom_prompt': contact_prompt_text_for_user(recipient) if recipient else '',
        'actor_custom_prompt': contact_prompt_text_for_user(actor) if actor else '',
    }


def _render_realtime_notification_message(
    *,
    event_type: str,
    task: Task,
    recipient: User | None,
    actor: User | None,
    fallback: str,
    previous_status: TaskStatus | None = None,
    changed_fields: list[str] | None = None,
) -> str:
    task_url = _task_url(task.id)
    if not is_bot_llm_configured():
        return ensure_task_link(fallback, task_url)

    event_payload = _task_notification_payload(
        event_type=event_type,
        task=task,
        recipient=recipient,
        actor=actor,
        previous_status=previous_status,
        changed_fields=changed_fields,
    )
    user_prompt = (
        'Viết một thông báo Zalo cho realtime task event dưới đây.\n'
        'Trả về duy nhất nội dung tin nhắn, không markdown fence, không JSON.\n\n'
        f'{json.dumps(event_payload, ensure_ascii=False, default=str)}'
    )
    system_prompt = notification_prompt_text()
    event_prompt = notification_event_prompt_text(event_type)
    if event_prompt:
        system_prompt = f'{system_prompt}\n\n# Prompt riêng cho event `{event_type}`\n{event_prompt}'
    try:
        message = generate_bot_reply(system_prompt=system_prompt, user_prompt=user_prompt).strip()
    except BotLLMError as exc:
        logger.warning('Failed to render notification with LLM for task_id=%s event=%s: %s', task.id, event_type, exc)
        return ensure_task_link(fallback, task_url)

    return ensure_task_link(message[:1200] if message else fallback, event_payload['task'].get('url') or task_url)


def _assigned_to_user_clause(user_id: str):
    if settings.database_url.startswith('sqlite'):
        return Task.assigned_to == user_id
    return cast(Task.assigned_to, String(64)) == user_id


def _active_admins(db: Session) -> list[User]:
    return db.scalars(
        select(User)
        .where(User.is_active.is_(True), func.lower(func.coalesce(User.role, '')) == 'admin')
        .order_by(func.lower(func.coalesce(User.full_name, User.username)).asc())
    ).all()


def enqueue_task_created_notifications(db: Session, task: Task) -> dict[str, int]:
    if not task.assigned_to:
        return {'created': 0, 'deduped': 0}

    assignee = task.assignee or db.get(User, task.assigned_to)
    target_id = assignee.zalo_user_id if assignee else None
    assignee_name = assignee.name if assignee else 'Bạn'

    fallback_message = (
        f'Chào {assignee_name} 👋\n'
        f'Bạn vừa được giao task mới: "{_task_title(task)}".\n'
        'Vào Task Manager để bắt đầu xử lý nha 💪'
    )
    message = _render_realtime_notification_message(
        event_type='task_assigned_on_create',
        task=task,
        recipient=assignee,
        actor=task.creator,
        fallback=fallback_message,
    )

    created_at = task.created_at or now_local()
    spec = NotificationSpec(
        event_key=f'task:{task.id}:create-assigned:{task.assigned_to}:{created_at.isoformat()}',
        event_type='task_assigned_on_create',
        channel=NotificationChannel.user,
        target_id=target_id,
        task_id=task.id,
        user_id=task.assigned_to,
        payload={
            'message': message,
            'context': {
                'source': 'realtime',
                'reason': 'task_created_assigned',
                'llm_rendered': message != fallback_message,
            },
        },
    )

    event, created = enqueue_notification_event(db, spec)
    db.commit()
    dispatch_due_notification_events(db, limit=1)
    return {'created': int(created), 'deduped': int(not created), 'event_id': event.id}


def enqueue_task_updated_notifications(
    db: Session,
    *,
    task: Task,
    actor: User,
    changed_fields: list[str],
) -> dict[str, int]:
    if not task.assigned_to or actor.id == task.assigned_to:
        return {'created': 0, 'deduped': 0}

    assignee = task.assignee or db.get(User, task.assigned_to)
    target_id = assignee.zalo_user_id if assignee else None
    field_text = ', '.join(changed_fields) if changed_fields else 'một vài thông tin'
    fallback_message = (
        f'{assignee.name if assignee else "Bạn"} ơi, task "{_task_title(task)}" vừa được cập nhật.\n'
        f'Mục thay đổi: {field_text}.'
    )
    message = _render_realtime_notification_message(
        event_type='task_updated',
        task=task,
        recipient=assignee,
        actor=actor,
        fallback=fallback_message,
        changed_fields=changed_fields,
    )
    event_time = task.updated_at or now_local()
    spec = NotificationSpec(
        event_key=f'task:{task.id}:updated:{task.assigned_to}:{event_time.isoformat()}',
        event_type='task_updated',
        channel=NotificationChannel.user,
        target_id=target_id,
        task_id=task.id,
        user_id=task.assigned_to,
        payload={
            'message': message,
            'context': {
                'source': 'realtime',
                'reason': 'task_updated',
                'changed_fields': changed_fields,
                'actor_id': actor.id,
                'llm_rendered': message != fallback_message,
            },
        },
    )
    event, created = enqueue_notification_event(db, spec)
    db.commit()
    dispatch_due_notification_events(db, limit=1)
    return {'created': int(created), 'deduped': int(not created), 'event_id': event.id}


def enqueue_task_deleted_notifications(db: Session, *, task: Task, actor: User) -> dict[str, int]:
    if not task.assigned_to or actor.id == task.assigned_to:
        return {'created': 0, 'deduped': 0}

    assignee = task.assignee or db.get(User, task.assigned_to)
    target_id = assignee.zalo_user_id if assignee else None
    fallback_message = (
        f'{assignee.name if assignee else "Bạn"} ơi, task "{_task_title(task)}" vừa được xoá bởi {actor.name}.'
    )
    message = _render_realtime_notification_message(
        event_type='task_deleted',
        task=task,
        recipient=assignee,
        actor=actor,
        fallback=fallback_message,
    )
    event_time = now_local()
    spec = NotificationSpec(
        event_key=f'task:{task.id}:deleted:{task.assigned_to}:{event_time.isoformat()}',
        event_type='task_deleted',
        channel=NotificationChannel.user,
        target_id=target_id,
        task_id=None,
        user_id=task.assigned_to,
        payload={
            'message': message,
            'context': {
                'source': 'realtime',
                'reason': 'task_deleted',
                'task_id': task.id,
                'actor_id': actor.id,
                'llm_rendered': message != fallback_message,
            },
        },
    )
    event, created = enqueue_notification_event(db, spec)
    db.commit()
    dispatch_due_notification_events(db, limit=1)
    return {'created': int(created), 'deduped': int(not created), 'event_id': event.id}


def enqueue_task_status_transition_notifications(
    db: Session,
    *,
    task: Task,
    previous_status: TaskStatus,
    actor: User,
) -> dict[str, int]:
    specs: list[NotificationSpec] = []
    current_status = task.status
    event_time = task.updated_at or now_local()

    if previous_status == TaskStatus.review and current_status == TaskStatus.ready and task.assigned_to:
        assignee = task.assignee or db.get(User, task.assigned_to)
        target_id = assignee.zalo_user_id if assignee else None
        assignee_name = assignee.name if assignee else 'Bạn'
        fallback_message = (
            f'Chúc mừng {assignee_name} 🎉\n'
            f'Task "{_task_title(task)}" đã được admin duyệt (ready).\n'
            'Bạn có thể triển khai tiếp ngay nha 😎'
        )
        message = _render_realtime_notification_message(
            event_type='task_approved_ready',
            task=task,
            recipient=assignee,
            actor=actor,
            fallback=fallback_message,
            previous_status=previous_status,
        )
        specs.append(
            NotificationSpec(
                event_key=f'task:{task.id}:review-ready:{task.assigned_to}:{event_time.isoformat()}',
                event_type='task_approved_ready',
                channel=NotificationChannel.user,
                target_id=target_id,
                task_id=task.id,
                user_id=task.assigned_to,
                payload={
                    'message': message,
                    'context': {'source': 'realtime', 'reason': 'review_to_ready', 'llm_rendered': message != fallback_message},
                },
            )
        )

    if current_status == TaskStatus.review:
        admins = _active_admins(db)
        for admin in admins:
            fallback_message = (
                f'Admin ơi 👑\n'
                f'Task "{_task_title(task)}" vừa được chuyển sang review.\n'
                'Vào duyệt giúp team khi rảnh nhé 🔍'
            )
            message = _render_realtime_notification_message(
                event_type='task_submitted_for_review',
                task=task,
                recipient=admin,
                actor=actor,
                fallback=fallback_message,
                previous_status=previous_status,
            )
            specs.append(
                NotificationSpec(
                    event_key=f'task:{task.id}:to-review:admin:{admin.id}:{event_time.isoformat()}',
                    event_type='task_submitted_for_review',
                    channel=NotificationChannel.user,
                    target_id=admin.zalo_user_id,
                    task_id=task.id,
                    user_id=admin.id,
                    payload={
                        'message': message,
                        'context': {'source': 'realtime', 'reason': 'moved_to_review', 'llm_rendered': message != fallback_message},
                    },
                )
            )

    actor_is_admin = (actor.role or '').lower() == 'admin'
    if current_status == TaskStatus.done and not actor_is_admin:
        admins = _active_admins(db)
        assignee_name = task.assignee.name if task.assignee else (task.assigned_to or 'member')
        for admin in admins:
            fallback_message = (
                f'Admin update ✅\n'
                f'Task "{_task_title(task)}" đã được {assignee_name} chuyển sang done.'
            )
            message = _render_realtime_notification_message(
                event_type='task_done_by_member',
                task=task,
                recipient=admin,
                actor=actor,
                fallback=fallback_message,
                previous_status=previous_status,
            )
            specs.append(
                NotificationSpec(
                    event_key=f'task:{task.id}:done-by-member:admin:{admin.id}:{event_time.isoformat()}',
                    event_type='task_done_by_member',
                    channel=NotificationChannel.user,
                    target_id=admin.zalo_user_id,
                    task_id=task.id,
                    user_id=admin.id,
                    payload={
                        'message': message,
                        'context': {'source': 'realtime', 'reason': 'member_done', 'llm_rendered': message != fallback_message},
                    },
                )
            )

    if not specs:
        return {'created': 0, 'deduped': 0}

    created = 0
    deduped = 0
    for spec in specs:
        _, inserted = enqueue_notification_event(db, spec)
        if inserted:
            created += 1
        else:
            deduped += 1

    db.commit()
    dispatch_due_notification_events(db, limit=min(20, settings.notification_delivery_batch_limit))
    return {'created': created, 'deduped': deduped}


def _tasks_today_for_user(db: Session, *, user_id: str, today: date) -> list[Task]:
    stmt = (
        select(Task)
        .options(joinedload(Task.task_type), joinedload(Task.shop))
        .where(_assigned_to_user_clause(user_id), Task.status != TaskStatus.done)
        .where(
            or_(
                Task.scheduled_date == today,
                Task.due_date == today,
                Task.due_date < today,
                and_(Task.scheduled_date.is_(None), Task.due_date.is_(None)),
            )
        )
        .order_by(Task.list_order.asc(), Task.created_at.asc())
    )
    return db.scalars(stmt).all()


def _approved_tasks_for_user(db: Session, *, user_id: str) -> list[Task]:
    stmt = (
        select(Task)
        .where(_assigned_to_user_clause(user_id), Task.status == TaskStatus.ready)
        .order_by(Task.updated_at.desc(), Task.id.desc())
    )
    return db.scalars(stmt).all()


def _pending_review_tasks(db: Session) -> list[Task]:
    stmt = (
        select(Task)
        .options(joinedload(Task.assignee))
        .where(Task.status == TaskStatus.review)
        .order_by(Task.updated_at.asc(), Task.id.asc())
    )
    return db.scalars(stmt).all()


def _all_active_users(db: Session) -> list[User]:
    return db.scalars(
        select(User)
        .where(User.is_active.is_(True))
        .order_by(func.lower(func.coalesce(User.full_name, User.username)).asc())
    ).all()


def _render_morning_group_message(user_summaries: list[dict[str, Any]]) -> str:
    template = _choose_template(MORNING_GROUP_TEMPLATES)
    return render_template(template, {'user_summaries': user_summaries})


def _render_morning_admin_message(pending_tasks: list[dict[str, Any]]) -> str:
    template = _choose_template(MORNING_ADMIN_TEMPLATES)
    return render_template(
        template,
        {
            'pending_count': len(pending_tasks),
            'pending_tasks': pending_tasks,
        },
    )


def _render_morning_user_message(name: str, today_tasks: list[dict[str, Any]], approved_tasks: list[dict[str, Any]]) -> str:
    template = _choose_template(MORNING_USER_TEMPLATES)
    approved_footer = (
        '👉 Có thể bắt đầu triển khai / đăng bài được rồi nha 😎'
        if approved_tasks
        else 'Chưa có task nào được duyệt, ráng làm xong rồi submit nha 💪'
    )
    fallback_approved = approved_tasks if approved_tasks else [{'task_title': '(Chưa có task nào được duyệt)'}]
    return render_template(
        template,
        {
            'name': name,
            'total_tasks': len(today_tasks),
            'today_tasks': today_tasks,
            'approved_tasks': fallback_approved,
            'approved_footer': approved_footer,
        },
    )


def _render_evening_group_message(total_done: int, total_pending: int) -> str:
    template = _choose_template(EVENING_GROUP_TEMPLATES)
    return render_template(template, {'total_done': total_done, 'total_pending': total_pending})


def _snapshot_is_due_today_or_overdue(task: DailyTaskSnapshot, today: date) -> bool:
    if task.done:
        return False
    return task.due_date is None or task.due_date <= today


def _snapshot_is_approved(task: DailyTaskSnapshot) -> bool:
    return task.status == TaskStatus.ready.value and not task.done


def _snapshot_task_description(task: DailyTaskSnapshot) -> str:
    due = task.due_date.strftime('%d/%m') if task.due_date else 'Anytime'
    parts = [f'Status: {task.status}', f'Due: {due}']
    if task.url:
        parts.append(f'Link task: {task.url}')
    return ' · '.join(parts)


def _build_vikunja_daily_specs(
    db: Session,
    *,
    job: Literal['morning', 'evening'],
    today: date,
    snapshots: list[DailyTaskSnapshot],
) -> list[NotificationSpec]:
    day_key = today.isoformat()

    if job == 'morning':
        users = _all_active_users(db)
        user_summaries: list[dict[str, Any]] = []
        per_user_specs: list[NotificationSpec] = []

        for user in users:
            today_tasks = [
                task for task in snapshots if user.id in task.assignee_user_ids and _snapshot_is_due_today_or_overdue(task, today)
            ]
            approved_tasks = [task for task in snapshots if user.id in task.assignee_user_ids and _snapshot_is_approved(task)]
            if today_tasks:
                user_summaries.append({'name': user.name, 'task_count': len(today_tasks)})
            if not today_tasks and not approved_tasks:
                continue

            message = _render_morning_user_message(
                user.name,
                [
                    {
                        'task_title': task.title,
                        'task_description': _snapshot_task_description(task),
                    }
                    for task in today_tasks
                ],
                [{'task_title': task.title} for task in approved_tasks],
            )
            per_user_specs.append(
                NotificationSpec(
                    event_key=f'vikunja:daily:morning:user:{user.id}:{day_key}',
                    event_type='daily_morning_user',
                    channel=NotificationChannel.user,
                    target_id=user.zalo_user_id,
                    user_id=user.id,
                    payload={
                        'message': message,
                        'context': {
                            'source': 'daily_job',
                            'task_source': 'vikunja',
                            'job': 'morning',
                            'scope': 'user',
                            'date': day_key,
                        },
                    },
                )
            )

        specs: list[NotificationSpec] = [
            NotificationSpec(
                event_key=f'vikunja:daily:morning:group:{day_key}',
                event_type='daily_morning_group',
                channel=NotificationChannel.group,
                target_id=settings.zalo_group_id,
                payload={
                    'message': _render_morning_group_message(user_summaries),
                    'context': {'source': 'daily_job', 'task_source': 'vikunja', 'job': 'morning', 'scope': 'group', 'date': day_key},
                },
            )
        ]

        review_tasks = [task for task in snapshots if task.status == TaskStatus.review.value and not task.done]
        pending_payload = [
            {
                'task_title': task.title,
                'assignee': ', '.join(task.assignee_names) or 'Unassigned',
            }
            for task in review_tasks
        ]
        for admin in _active_admins(db):
            specs.append(
                NotificationSpec(
                    event_key=f'vikunja:daily:morning:admin:{admin.id}:{day_key}',
                    event_type='daily_morning_admin',
                    channel=NotificationChannel.user,
                    target_id=admin.zalo_user_id,
                    user_id=admin.id,
                    payload={
                        'message': _render_morning_admin_message(pending_payload),
                        'context': {
                            'source': 'daily_job',
                            'task_source': 'vikunja',
                            'job': 'morning',
                            'scope': 'admin',
                            'date': day_key,
                        },
                    },
                )
            )

        specs.extend(per_user_specs)
        return specs

    total_done = len(
        [
            task
            for task in snapshots
            if task.done and task.updated_at and task.updated_at.astimezone(ZoneInfo(settings.notify_timezone)).date() == today
        ]
    )
    total_pending = len([task for task in snapshots if not task.done])
    return [
        NotificationSpec(
            event_key=f'vikunja:daily:evening:group:{day_key}',
            event_type='daily_evening_group',
            channel=NotificationChannel.group,
            target_id=settings.zalo_group_id,
            payload={
                'message': _render_evening_group_message(total_done=total_done, total_pending=total_pending),
                'context': {'source': 'daily_job', 'task_source': 'vikunja', 'job': 'evening', 'scope': 'group', 'date': day_key},
            },
        )
    ]


def _build_daily_specs(db: Session, *, job: Literal['morning', 'evening'], today: date) -> list[NotificationSpec]:
    day_key = today.isoformat()
    vikunja_snapshots = _fetch_vikunja_daily_snapshots(db)
    if vikunja_snapshots is not None:
        return _build_vikunja_daily_specs(db, job=job, today=today, snapshots=vikunja_snapshots)

    if job == 'morning':
        users = _all_active_users(db)

        user_summaries: list[dict[str, Any]] = []
        per_user_specs: list[NotificationSpec] = []
        for user in users:
            today_tasks = _tasks_today_for_user(db, user_id=user.id, today=today)
            if today_tasks:
                user_summaries.append({'name': user.name, 'task_count': len(today_tasks)})

            approved_tasks = _approved_tasks_for_user(db, user_id=user.id)
            if not today_tasks and not approved_tasks:
                continue

            message = _render_morning_user_message(
                user.name,
                [
                    {
                        'task_title': task.title,
                        'task_description': task.description or 'Không có mô tả',
                    }
                    for task in today_tasks
                ],
                [{'task_title': task.title} for task in approved_tasks],
            )

            per_user_specs.append(
                NotificationSpec(
                    event_key=f'daily:morning:user:{user.id}:{day_key}',
                    event_type='daily_morning_user',
                    channel=NotificationChannel.user,
                    target_id=user.zalo_user_id,
                    user_id=user.id,
                    payload={
                        'message': message,
                        'context': {'source': 'daily_job', 'job': 'morning', 'scope': 'user', 'date': day_key},
                    },
                )
            )

        specs: list[NotificationSpec] = []
        group_message = _render_morning_group_message(user_summaries)
        specs.append(
            NotificationSpec(
                event_key=f'daily:morning:group:{day_key}',
                event_type='daily_morning_group',
                channel=NotificationChannel.group,
                target_id=settings.zalo_group_id,
                payload={
                    'message': group_message,
                    'context': {'source': 'daily_job', 'job': 'morning', 'scope': 'group', 'date': day_key},
                },
            )
        )

        pending_tasks = _pending_review_tasks(db)
        pending_payload = [
            {
                'task_title': task.title,
                'assignee': task.assignee.name if task.assignee else 'Unassigned',
            }
            for task in pending_tasks
        ]
        admins = _active_admins(db)
        for admin in admins:
            specs.append(
                NotificationSpec(
                    event_key=f'daily:morning:admin:{admin.id}:{day_key}',
                    event_type='daily_morning_admin',
                    channel=NotificationChannel.user,
                    target_id=admin.zalo_user_id,
                    user_id=admin.id,
                    payload={
                        'message': _render_morning_admin_message(pending_payload),
                        'context': {'source': 'daily_job', 'job': 'morning', 'scope': 'admin', 'date': day_key},
                    },
                )
            )

        specs.extend(per_user_specs)
        return specs

    done_stmt = select(func.count(Task.id)).where(
        Task.status == TaskStatus.done,
        func.date(Task.updated_at) == today,
    )
    pending_stmt = select(func.count(Task.id)).where(Task.status != TaskStatus.done)

    total_done = int(db.scalar(done_stmt) or 0)
    total_pending = int(db.scalar(pending_stmt) or 0)

    return [
        NotificationSpec(
            event_key=f'daily:evening:group:{day_key}',
            event_type='daily_evening_group',
            channel=NotificationChannel.group,
            target_id=settings.zalo_group_id,
            payload={
                'message': _render_evening_group_message(total_done=total_done, total_pending=total_pending),
                'context': {'source': 'daily_job', 'job': 'evening', 'scope': 'group', 'date': day_key},
            },
        )
    ]


def run_daily_notification_job(db: Session, *, job: Literal['morning', 'evening']) -> dict[str, Any]:
    today = now_local().date()
    specs = _build_daily_specs(db, job=job, today=today)

    created = 0
    deduped = 0
    for spec in specs:
        _, inserted = enqueue_notification_event(db, spec)
        if inserted:
            created += 1
        else:
            deduped += 1

    db.commit()

    dispatch_stats = dispatch_due_notification_events(db)
    return {
        'job': job,
        'date': today.isoformat(),
        'events_total': len(specs),
        'events_created': created,
        'events_deduped': deduped,
        'dispatch': dispatch_stats,
    }
