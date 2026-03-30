from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
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

from .config import get_settings
from .models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationEvent,
    NotificationStatus,
    Task,
    TaskStatus,
    User,
)

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


def _call_worker(request_payload: dict[str, Any]) -> tuple[bool, int | None, str | None, str | None]:
    worker_url = _resolve_worker_send_url()
    if not worker_url:
        return False, None, None, 'ZALO_WORKER_URL is not configured.'

    headers = {'Content-Type': 'application/json'}
    if settings.zalo_worker_token:
        headers['Authorization'] = f'Bearer {settings.zalo_worker_token}'
    if settings.zalo_shared_secret:
        headers['X-Internal-Secret'] = settings.zalo_shared_secret

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
    next_attempt = event.attempt_count + 1

    delivery = NotificationDelivery(
        event_id=event.id,
        attempt=next_attempt,
        request_payload=request_payload,
    )

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

    message = (
        f'Chào {assignee_name} 👋\n'
        f'Bạn vừa được giao task mới: "{_task_title(task)}".\n'
        'Vào Task Manager để bắt đầu xử lý nha 💪'
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
        specs.append(
            NotificationSpec(
                event_key=f'task:{task.id}:review-ready:{task.assigned_to}:{event_time.isoformat()}',
                event_type='task_approved_ready',
                channel=NotificationChannel.user,
                target_id=target_id,
                task_id=task.id,
                user_id=task.assigned_to,
                payload={
                    'message': (
                        f'Chúc mừng {assignee_name} 🎉\n'
                        f'Task "{_task_title(task)}" đã được admin duyệt (ready).\n'
                        'Bạn có thể triển khai tiếp ngay nha 😎'
                    ),
                    'context': {'source': 'realtime', 'reason': 'review_to_ready'},
                },
            )
        )

    if current_status == TaskStatus.review:
        admins = _active_admins(db)
        for admin in admins:
            specs.append(
                NotificationSpec(
                    event_key=f'task:{task.id}:to-review:admin:{admin.id}:{event_time.isoformat()}',
                    event_type='task_submitted_for_review',
                    channel=NotificationChannel.user,
                    target_id=admin.zalo_user_id,
                    task_id=task.id,
                    user_id=admin.id,
                    payload={
                        'message': (
                            f'Admin ơi 👑\n'
                            f'Task "{_task_title(task)}" vừa được chuyển sang review.\n'
                            'Vào duyệt giúp team khi rảnh nhé 🔍'
                        ),
                        'context': {'source': 'realtime', 'reason': 'moved_to_review'},
                    },
                )
            )

    actor_is_admin = (actor.role or '').lower() == 'admin'
    if current_status == TaskStatus.done and not actor_is_admin:
        admins = _active_admins(db)
        assignee_name = task.assignee.name if task.assignee else (task.assigned_to or 'member')
        for admin in admins:
            specs.append(
                NotificationSpec(
                    event_key=f'task:{task.id}:done-by-member:admin:{admin.id}:{event_time.isoformat()}',
                    event_type='task_done_by_member',
                    channel=NotificationChannel.user,
                    target_id=admin.zalo_user_id,
                    task_id=task.id,
                    user_id=admin.id,
                    payload={
                        'message': (
                            f'Admin update ✅\n'
                            f'Task "{_task_title(task)}" đã được {assignee_name} chuyển sang done.'
                        ),
                        'context': {'source': 'realtime', 'reason': 'member_done'},
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


def _build_daily_specs(db: Session, *, job: Literal['morning', 'evening'], today: date) -> list[NotificationSpec]:
    day_key = today.isoformat()

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
