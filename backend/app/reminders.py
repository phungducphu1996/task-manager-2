from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hmac
import re
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .bot_files import contact_prompt_text_for_group, contact_prompt_text_for_user
from .bot_llm import BotLLMError, generate_bot_reply, is_bot_llm_configured
from .config import get_settings
from .models import (
    NotificationChannel,
    NotificationEvent,
    ReminderInteraction,
    ReminderInteractionType,
    ReminderRule,
    ReminderRuleType,
    ReminderRun,
    ReminderRunStatus,
    ReminderScheduleType,
    Task,
    TaskComment,
    TaskStatus,
    User,
)
from .notifications import NotificationSpec, dispatch_due_notification_events, enqueue_notification_event

settings = get_settings()


@dataclass(slots=True)
class ReminderInteractionResult:
    handled: bool
    interaction_type: ReminderInteractionType
    message: str
    run_id: int | None = None


def now_reminder() -> datetime:
    return datetime.now(ZoneInfo(settings.reminder_timezone))


def is_reminder_internal_token_valid(token: str | None) -> bool:
    expected = settings.reminder_tick_internal_token or settings.notify_internal_token
    if not expected or not token:
        return False
    return hmac.compare_digest(token, expected)


def reminder_internal_token_configured() -> bool:
    return bool(settings.reminder_tick_internal_token or settings.notify_internal_token)


def _parse_time_token(value: str, *, fallback: time) -> time:
    try:
        hour, minute = value.split(':', 1)
        return time(hour=int(hour), minute=int(minute))
    except (ValueError, TypeError):
        return fallback


def default_quiet_start() -> time:
    return _parse_time_token(settings.reminder_default_quiet_start, fallback=time(22, 0))


def default_quiet_end() -> time:
    return _parse_time_token(settings.reminder_default_quiet_end, fallback=time(7, 0))


def _ensure_aware(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    return start, start + timedelta(days=1)


def _is_quiet(moment: datetime, start: time, end: time) -> bool:
    current = moment.time()
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _shift_out_of_quiet(moment: datetime, start: time, end: time) -> datetime:
    if not _is_quiet(moment, start, end):
        return moment
    target_date = moment.date() + timedelta(days=1) if start > end and moment.time() >= start else moment.date()
    return datetime.combine(target_date, end, tzinfo=moment.tzinfo)


def _task_stopped(rule: ReminderRule) -> bool:
    if not rule.task:
        return False
    stop_statuses = {str(item) for item in (rule.stop_statuses or [])}
    return rule.task.status.value in stop_statuses


def _runs_today_count(db: Session, *, rule: ReminderRule, now: datetime) -> int:
    start, end = _day_bounds(now)
    return int(
        db.scalar(
            select(func.count(ReminderRun.id)).where(
                ReminderRun.rule_id == rule.id,
                ReminderRun.scheduled_for >= start,
                ReminderRun.scheduled_for < end,
            )
        )
        or 0
    )


def _last_run(db: Session, rule_id: int) -> ReminderRun | None:
    return db.scalar(
        select(ReminderRun)
        .where(ReminderRun.rule_id == rule_id)
        .order_by(ReminderRun.scheduled_for.desc(), ReminderRun.id.desc())
    )


def _daily_due_time(rule: ReminderRule, now: datetime) -> datetime | None:
    schedule_time = rule.schedule_time or time(9, 0)
    scheduled = datetime.combine(now.date(), schedule_time, tzinfo=now.tzinfo)
    scheduled = _shift_out_of_quiet(
        scheduled,
        rule.quiet_start or default_quiet_start(),
        rule.quiet_end or default_quiet_end(),
    )
    return scheduled if scheduled <= now else None


def _interval_due_time(db: Session, rule: ReminderRule, now: datetime) -> datetime | None:
    interval = rule.interval_minutes or 60
    last = _last_run(db, rule.id)
    if last and last.snoozed_until:
        due = _ensure_aware(last.snoozed_until, ZoneInfo(rule.timezone or settings.reminder_timezone))
    elif last:
        due = _ensure_aware(last.scheduled_for, ZoneInfo(rule.timezone or settings.reminder_timezone)) + timedelta(
            minutes=interval
        )
    else:
        due = _ensure_aware(rule.created_at, ZoneInfo(rule.timezone or settings.reminder_timezone))

    due = _shift_out_of_quiet(due, rule.quiet_start or default_quiet_start(), rule.quiet_end or default_quiet_end())
    return due if due <= now else None


def _due_time_for_rule(db: Session, rule: ReminderRule, now: datetime) -> datetime | None:
    if not rule.enabled or _task_stopped(rule):
        return None
    rule_tz = ZoneInfo(rule.timezone or settings.reminder_timezone)
    local_now = now.astimezone(rule_tz)
    max_runs = rule.max_runs_per_day
    if max_runs and _runs_today_count(db, rule=rule, now=local_now) >= max_runs:
        return None
    if rule.schedule_type == ReminderScheduleType.interval:
        return _interval_due_time(db, rule, local_now)
    return _daily_due_time(rule, local_now)


def _assigned_to_user_clause(user_id: str):
    if settings.database_url.startswith('sqlite'):
        return Task.assigned_to == user_id
    return cast(Task.assigned_to, String(64)) == user_id


def _active_users(db: Session) -> list[User]:
    return db.scalars(
        select(User)
        .where(User.is_active.is_(True))
        .order_by(func.lower(func.coalesce(User.full_name, User.username)).asc())
    ).all()


def _active_admins(db: Session) -> list[User]:
    return db.scalars(
        select(User)
        .where(User.is_active.is_(True), func.lower(func.coalesce(User.role, '')) == 'admin')
        .order_by(func.lower(func.coalesce(User.full_name, User.username)).asc())
    ).all()


def _tasks_today(db: Session, *, today: date) -> list[Task]:
    return db.scalars(
        select(Task)
        .options(joinedload(Task.assignee), joinedload(Task.shop), joinedload(Task.task_type))
        .where(Task.status != TaskStatus.done, Task.due_date == today)
        .order_by(Task.due_date.asc(), Task.updated_at.asc(), Task.id.asc())
    ).unique().all()


def _tasks_overdue(db: Session, *, today: date) -> list[Task]:
    return db.scalars(
        select(Task)
        .options(joinedload(Task.assignee), joinedload(Task.shop), joinedload(Task.task_type))
        .where(Task.status != TaskStatus.done, Task.due_date.is_not(None), Task.due_date < today)
        .order_by(Task.due_date.asc(), Task.id.asc())
    ).unique().all()


def _tasks_for_user_today(db: Session, *, user: User, today: date) -> list[Task]:
    return db.scalars(
        select(Task)
        .options(joinedload(Task.assignee), joinedload(Task.shop), joinedload(Task.task_type))
        .where(Task.status != TaskStatus.done, _assigned_to_user_clause(user.id))
        .where(or_(Task.due_date == today, Task.due_date < today))
        .order_by(Task.due_date.asc(), Task.id.asc())
    ).unique().all()


def _pending_review_tasks(db: Session) -> list[Task]:
    return db.scalars(
        select(Task)
        .options(joinedload(Task.assignee), joinedload(Task.shop), joinedload(Task.task_type))
        .where(Task.status == TaskStatus.review)
        .order_by(Task.updated_at.asc(), Task.id.asc())
    ).unique().all()


def _task_line(task: Task) -> str:
    assignee = task.assignee.name if task.assignee else 'Unassigned'
    due = task.due_date.strftime('%d/%m') if task.due_date else 'no due'
    return f'• #{task.id} {task.title} — {assignee} — {task.status.value} — {due}'


def _render_group_digest(db: Session, *, now: datetime, rule: ReminderRule) -> str:
    today = now.date()
    today_tasks = _tasks_today(db, today=today)
    overdue_tasks = _tasks_overdue(db, today=today)
    review_tasks = _pending_review_tasks(db)
    users = _active_users(db)

    workload: list[str] = []
    for user in users:
        count = len(_tasks_for_user_today(db, user=user, today=today))
        if count:
            workload.append(f'• {user.name}: {count} task')

    group_prompt = ''
    if rule.target_channel == NotificationChannel.group and rule.target_id:
        group_prompt = contact_prompt_text_for_group(rule.target_id, rule.payload.get('group_name') or 'Zalo group')

    lines = [
        'Chào cả nhà, tổng quan task hôm nay nè:',
        f'• Hôm nay: {len(today_tasks)} task',
        f'• Quá hạn: {len(overdue_tasks)} task',
        f'• Chờ review: {len(review_tasks)} task',
    ]
    if workload:
        lines.append('\nWorkload:')
        lines.extend(workload[:8])
    if overdue_tasks:
        lines.append('\nQuá hạn cần để ý:')
        lines.extend(_task_line(task) for task in overdue_tasks[:5])
    if group_prompt:
        lines.append('\nTone note: đã dùng style group.')
    return '\n'.join(lines)


def _render_member_checkin(db: Session, *, user: User, now: datetime) -> str:
    tasks = _tasks_for_user_today(db, user=user, today=now.date())
    custom = contact_prompt_text_for_user(user)
    lines = [f'Chào {user.name} 👋', 'Hôm nay mình check-in nhẹ nha.']
    if tasks:
        lines.append('\nTask của bạn:')
        lines.extend(_task_line(task) for task in tasks[:10])
    else:
        lines.append('\nHiện chưa thấy task due hôm nay/quá hạn của bạn.')
    lines.append('\nReply "ok", "đang làm", hoặc nếu bị kẹt thì nói em biết để báo admin nha.')
    if custom:
        lines.append('\nTone note: đã dùng style cá nhân.')
    return '\n'.join(lines)


def _render_task_nudge(rule: ReminderRule) -> str:
    task = rule.task
    if not task:
        return 'Nhắc task: task này không còn tồn tại.'
    due = task.due_date.strftime('%d/%m/%Y') if task.due_date else 'chưa có deadline'
    return (
        f'Nhắc nhẹ task này nha:\n'
        f'{_task_line(task)}\n'
        f'Deadline: {due}\n'
        'Reply "ok" nếu đã nhận, hoặc nói blocker nếu đang kẹt.'
    )


def _strategy_fallback(db: Session, *, now: datetime) -> str:
    today = now.date()
    today_tasks = _tasks_today(db, today=today)
    overdue_tasks = _tasks_overdue(db, today=today)
    review_tasks = _pending_review_tasks(db)
    lines = [
        'Gợi ý hướng làm hôm nay:',
        f'• Ưu tiên xử lý {len(overdue_tasks)} task quá hạn trước.',
        f'• Chốt {len(review_tasks)} task đang review để team không bị nghẽn.',
        f'• Sau đó chia {len(today_tasks)} task due hôm nay theo từng người.',
    ]
    if overdue_tasks:
        lines.append('\nTop overdue:')
        lines.extend(_task_line(task) for task in overdue_tasks[:5])
    return '\n'.join(lines)


def _render_daily_strategy(db: Session, *, now: datetime) -> str:
    fallback = _strategy_fallback(db, now=now)
    if not is_bot_llm_configured():
        return fallback

    today = now.date()
    context = {
        'today': today.isoformat(),
        'today_tasks': [_task_line(task) for task in _tasks_today(db, today=today)[:12]],
        'overdue_tasks': [_task_line(task) for task in _tasks_overdue(db, today=today)[:12]],
        'review_tasks': [_task_line(task) for task in _pending_review_tasks(db)[:12]],
    }
    prompt = (
        'Bạn là Hazel Office Bot. Dựa vào context task thật, gợi ý chiến lược làm việc hôm nay. '
        'Trả lời tiếng Việt, ngắn, thực dụng, không bịa task.\n\n'
        f'{context}'
    )
    try:
        return generate_bot_reply(system_prompt='Hazel reminder strategy.', user_prompt=prompt).strip() or fallback
    except BotLLMError:
        return fallback


def _targets_for_rule(db: Session, rule: ReminderRule) -> list[tuple[NotificationChannel, str | None, User | None]]:
    if rule.rule_type == ReminderRuleType.daily_member_checkin and not rule.user_id:
        return [(NotificationChannel.user, user.zalo_user_id, user) for user in _active_users(db) if user.zalo_user_id]

    if rule.user_id:
        user = rule.user or db.get(User, rule.user_id)
        return [(NotificationChannel.user, user.zalo_user_id if user else None, user)]

    if rule.target_channel:
        return [(rule.target_channel, rule.target_id, None)]

    if rule.rule_type == ReminderRuleType.daily_group_digest:
        return [(NotificationChannel.group, settings.zalo_group_id, None)]

    if rule.rule_type == ReminderRuleType.daily_strategy:
        admins = _active_admins(db)
        return [(NotificationChannel.user, admin.zalo_user_id, admin) for admin in admins if admin.zalo_user_id]

    if rule.task and rule.task.assignee and rule.task.assignee.zalo_user_id:
        return [(NotificationChannel.user, rule.task.assignee.zalo_user_id, rule.task.assignee)]

    return []


def _message_for_rule(db: Session, rule: ReminderRule, *, now: datetime, user: User | None) -> str:
    if rule.rule_type == ReminderRuleType.daily_group_digest:
        return _render_group_digest(db, now=now, rule=rule)
    if rule.rule_type == ReminderRuleType.daily_member_checkin:
        if not user:
            return 'Check-in hôm nay: chưa xác định được user.'
        return _render_member_checkin(db, user=user, now=now)
    if rule.rule_type == ReminderRuleType.daily_strategy:
        return _render_daily_strategy(db, now=now)
    return _render_task_nudge(rule)


def _create_run_and_event(
    db: Session,
    *,
    rule: ReminderRule,
    scheduled_for: datetime,
    target_channel: NotificationChannel,
    target_id: str | None,
    user: User | None,
    now: datetime,
) -> tuple[ReminderRun, bool]:
    target_key = user.id if user else target_id or 'missing-target'
    run_key = f'reminder:{rule.id}:{target_key}:{scheduled_for.strftime("%Y%m%d%H%M")}'
    existing = db.scalar(select(ReminderRun).where(ReminderRun.run_key == run_key))
    if existing:
        return existing, False

    run = ReminderRun(
        rule_id=rule.id,
        scheduled_for=scheduled_for,
        status=ReminderRunStatus.pending,
        run_key=run_key,
    )
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(ReminderRun).where(ReminderRun.run_key == run_key))
        if existing:
            return existing, False
        raise

    message = _message_for_rule(db, rule, now=now, user=user)
    event, _ = enqueue_notification_event(
        db,
        NotificationSpec(
            event_key=f'{run_key}:delivery',
            event_type=f'reminder_{rule.rule_type.value}',
            channel=target_channel,
            target_id=target_id,
            task_id=rule.task_id,
            user_id=user.id if user else rule.user_id,
            payload={
                'message': message,
                'context': {
                    'source': 'reminder_engine',
                    'rule_id': rule.id,
                    'run_id': run.id,
                    'rule_type': rule.rule_type.value,
                },
            },
        ),
    )
    run.notification_event_id = event.id
    run.status = ReminderRunStatus.skipped if not target_id else ReminderRunStatus.sent
    db.add(run)
    return run, True


def _escalation_due(run: ReminderRun, now: datetime) -> bool:
    rule = run.rule
    if run.escalated_at or run.acknowledged_at or run.status in {ReminderRunStatus.acknowledged, ReminderRunStatus.blocked}:
        return False
    if rule.escalation_after_minutes:
        scheduled = _ensure_aware(run.scheduled_for, now.tzinfo or ZoneInfo(settings.reminder_timezone))
        return scheduled + timedelta(minutes=rule.escalation_after_minutes) <= now
    if rule.escalation_after_runs:
        count = len([item for item in rule.runs if item.created_at <= run.created_at])
        return count >= rule.escalation_after_runs
    return False


def _enqueue_admin_alert(db: Session, *, event_key: str, message: str, task_id: int | None = None) -> int:
    created = 0
    for admin in _active_admins(db):
        event, inserted = enqueue_notification_event(
            db,
            NotificationSpec(
                event_key=f'{event_key}:admin:{admin.id}',
                event_type='reminder_admin_alert',
                channel=NotificationChannel.user,
                target_id=admin.zalo_user_id,
                task_id=task_id,
                user_id=admin.id,
                payload={'message': message, 'context': {'source': 'reminder_engine', 'alert': True}},
            ),
        )
        created += int(inserted and event.status.value != 'skipped')
    return created


def _process_escalations(db: Session, *, now: datetime) -> int:
    runs = db.scalars(
        select(ReminderRun)
        .options(joinedload(ReminderRun.rule).joinedload(ReminderRule.task), joinedload(ReminderRun.rule).joinedload(ReminderRule.runs))
        .where(ReminderRun.escalated_at.is_(None))
        .where(ReminderRun.acknowledged_at.is_(None))
        .order_by(ReminderRun.scheduled_for.asc())
        .limit(100)
    ).unique().all()
    created = 0
    for run in runs:
        if not _escalation_due(run, now):
            continue
        rule = run.rule
        message = f'Admin ơi, reminder "{rule.name}" chưa được phản hồi đúng hạn.'
        if rule.task:
            message += f'\nTask liên quan: #{rule.task.id} {rule.task.title}'
        created += _enqueue_admin_alert(db, event_key=f'reminder:{run.id}:escalation', message=message, task_id=rule.task_id)
        run.escalated_at = now
        run.status = ReminderRunStatus.escalated
        db.add(run)
    return created


def run_reminder_tick(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    current = now or now_reminder()
    rules = db.scalars(
        select(ReminderRule)
        .options(joinedload(ReminderRule.task).joinedload(Task.assignee), joinedload(ReminderRule.user))
        .where(ReminderRule.enabled.is_(True))
        .order_by(ReminderRule.id.asc())
    ).unique().all()

    runs_created = 0
    runs_deduped = 0
    for rule in rules:
        due = _due_time_for_rule(db, rule, current)
        if not due:
            continue
        for target_channel, target_id, user in _targets_for_rule(db, rule):
            _, created = _create_run_and_event(
                db,
                rule=rule,
                scheduled_for=due,
                target_channel=target_channel,
                target_id=target_id,
                user=user,
                now=current,
            )
            if created:
                runs_created += 1
            else:
                runs_deduped += 1

    escalations_created = _process_escalations(db, now=current)
    db.commit()
    dispatch = dispatch_due_notification_events(db)
    return {
        'now': current.isoformat(),
        'rules_checked': len(rules),
        'runs_created': runs_created,
        'runs_deduped': runs_deduped,
        'escalations_created': escalations_created,
        'dispatch': dispatch,
    }


def create_reminder_rule(db: Session, *, actor: User, values: dict[str, Any]) -> ReminderRule:
    rule_type = ReminderRuleType(values['rule_type'])
    schedule_type = ReminderScheduleType(values.get('schedule_type') or ReminderScheduleType.daily.value)
    rule = ReminderRule(
        name=str(values.get('name') or rule_type.value),
        rule_type=rule_type,
        enabled=bool(values.get('enabled', True)),
        target_channel=values.get('target_channel'),
        target_id=values.get('target_id'),
        user_id=values.get('user_id'),
        task_id=values.get('task_id'),
        schedule_type=schedule_type,
        schedule_time=values.get('schedule_time'),
        interval_minutes=values.get('interval_minutes'),
        timezone=values.get('timezone') or settings.reminder_timezone,
        quiet_start=values.get('quiet_start') or default_quiet_start(),
        quiet_end=values.get('quiet_end') or default_quiet_end(),
        max_runs_per_day=values.get('max_runs_per_day'),
        stop_statuses=[item.value if isinstance(item, TaskStatus) else str(item) for item in values.get('stop_statuses') or []],
        escalation_after_minutes=values.get('escalation_after_minutes'),
        escalation_after_runs=values.get('escalation_after_runs'),
        payload=values.get('payload') or {},
        created_by=actor.id,
    )
    if rule.rule_type == ReminderRuleType.task_nudge and not rule.max_runs_per_day:
        rule.max_runs_per_day = settings.reminder_task_nudge_max_per_day
    if rule.rule_type == ReminderRuleType.task_nudge and not rule.stop_statuses:
        rule.stop_statuses = [TaskStatus.review.value, TaskStatus.ready.value, TaskStatus.done.value]
    if rule.rule_type == ReminderRuleType.task_nudge and not rule.escalation_after_runs:
        rule.escalation_after_runs = 3
    if rule.rule_type == ReminderRuleType.daily_member_checkin and not rule.escalation_after_minutes:
        rule.escalation_after_minutes = settings.reminder_member_checkin_admin_delay_minutes
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def update_reminder_rule(db: Session, *, rule: ReminderRule, values: dict[str, Any]) -> ReminderRule:
    for field, value in values.items():
        if field == 'stop_statuses' and value is not None:
            value = [item.value if isinstance(item, TaskStatus) else str(item) for item in value]
        if field in {'rule_type'}:
            continue
        setattr(rule, field, value)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def create_task_nudge_rule(
    db: Session,
    *,
    actor: User,
    task: Task,
    interval_minutes: int = 60,
    name: str | None = None,
) -> ReminderRule:
    return create_reminder_rule(
        db,
        actor=actor,
        values={
            'name': name or f'Nhắc task #{task.id}',
            'rule_type': ReminderRuleType.task_nudge.value,
            'target_channel': NotificationChannel.user,
            'task_id': task.id,
            'user_id': task.assigned_to,
            'schedule_type': ReminderScheduleType.interval.value,
            'interval_minutes': interval_minutes,
            'max_runs_per_day': settings.reminder_task_nudge_max_per_day,
            'stop_statuses': [TaskStatus.review, TaskStatus.ready, TaskStatus.done],
            'escalation_after_runs': 3,
        },
    )


def _normalize_text(text: str) -> str:
    replacements = {
        'đ': 'd',
        'Đ': 'd',
    }
    lowered = ''.join(replacements.get(ch, ch) for ch in text.lower())
    return re.sub(r'[^a-z0-9\s]', ' ', lowered)


def _classify_interaction(text: str) -> tuple[ReminderInteractionType, dict[str, Any]] | None:
    normalized = _normalize_text(text)
    if any(token in normalized.split() for token in {'ok', 'nhan', 'done'}):
        return ReminderInteractionType.ack, {}
    if any(phrase in normalized for phrase in {'dang lam', 'em lam day', 'da nhan'}):
        return ReminderInteractionType.ack, {}
    if any(phrase in normalized for phrase in {'ket brief', 'thieu file', 'chua co anh', 'doi feedback', 'bi ket'}):
        return ReminderInteractionType.blocker, {}
    if 'nhac lai' in normalized or 'de ' in normalized:
        hours = 2
        match = re.search(r'(\d+)\s*(h|gio|hour)', normalized)
        if match:
            hours = max(1, min(int(match.group(1)), 24))
        if 'mai' in normalized:
            return ReminderInteractionType.snooze, {'snooze_hours': 24}
        if 'chieu' in normalized:
            return ReminderInteractionType.snooze, {'snooze_until_hour': 15}
        return ReminderInteractionType.snooze, {'snooze_hours': hours}
    return None


def _snooze_until(now: datetime, payload: dict[str, Any]) -> datetime:
    if 'snooze_until_hour' in payload:
        target = datetime.combine(now.date(), time(hour=int(payload['snooze_until_hour'])), tzinfo=now.tzinfo)
        return target if target > now else target + timedelta(days=1)
    return now + timedelta(hours=int(payload.get('snooze_hours') or 2))


def _latest_active_run(
    db: Session,
    *,
    actor: User,
    conversation_id: str | None,
    target_id: str | None,
    now: datetime,
) -> ReminderRun | None:
    since = now - timedelta(days=2)
    stmt = (
        select(ReminderRun)
        .join(ReminderRule, ReminderRun.rule_id == ReminderRule.id)
        .outerjoin(NotificationEvent, ReminderRun.notification_event_id == NotificationEvent.id)
        .options(joinedload(ReminderRun.rule).joinedload(ReminderRule.task))
        .where(ReminderRun.created_at >= since)
        .where(ReminderRun.acknowledged_at.is_(None))
        .where(ReminderRun.status.in_([ReminderRunStatus.sent, ReminderRunStatus.pending, ReminderRunStatus.snoozed]))
    )
    conditions = [
        ReminderRule.user_id == actor.id,
        NotificationEvent.user_id == actor.id,
    ]
    if actor.zalo_user_id:
        conditions.append(NotificationEvent.target_id == actor.zalo_user_id)
    if target_id:
        conditions.append(NotificationEvent.target_id == target_id)
    if conversation_id:
        conditions.append(NotificationEvent.target_id == conversation_id)
    stmt = stmt.where(or_(*conditions)).order_by(ReminderRun.created_at.desc(), ReminderRun.id.desc())
    return db.scalar(stmt)


def handle_reminder_interaction(
    db: Session,
    *,
    actor: User,
    text: str,
    conversation_id: str | None,
    message_id: str | None,
    target_id: str | None,
) -> ReminderInteractionResult | None:
    classified = _classify_interaction(text)
    if not classified:
        return None

    current = now_reminder()
    run = _latest_active_run(db, actor=actor, conversation_id=conversation_id, target_id=target_id, now=current)
    if not run:
        return None

    interaction_type, payload = classified
    interaction = ReminderInteraction(
        run_id=run.id,
        rule_id=run.rule_id,
        user_id=actor.id,
        conversation_id=conversation_id,
        message_id=message_id,
        interaction_type=interaction_type,
        text=text,
        payload=payload,
    )
    db.add(interaction)

    if interaction_type == ReminderInteractionType.ack:
        run.acknowledged_at = current
        run.status = ReminderRunStatus.acknowledged
        message = 'Em ghi nhận rồi nha.'
    elif interaction_type == ReminderInteractionType.snooze:
        run.snoozed_until = _snooze_until(current, payload)
        run.status = ReminderRunStatus.snoozed
        message = f'Em dời lịch nhắc lại tới {run.snoozed_until.strftime("%d/%m %H:%M")} nha.'
    else:
        run.status = ReminderRunStatus.blocked
        message = 'Em ghi nhận blocker rồi, em sẽ báo admin để gỡ giúp nha.'
        if run.rule.task_id:
            db.add(
                TaskComment(
                    task_id=run.rule.task_id,
                    author_id=actor.id,
                    content=f'System reminder blocker từ {actor.name}: {text}',
                    mentions=[],
                )
            )
        _enqueue_admin_alert(
            db,
            event_key=f'reminder:{run.id}:blocker:{message_id or "message"}',
            message=f'{actor.name} báo blocker từ reminder "{run.rule.name}":\n{text}',
            task_id=run.rule.task_id,
        )

    db.add(run)
    db.commit()
    if interaction_type == ReminderInteractionType.blocker:
        dispatch_due_notification_events(db)
    return ReminderInteractionResult(
        handled=True,
        interaction_type=interaction_type,
        message=message,
        run_id=run.id,
    )
