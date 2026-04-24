from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import hmac
import re
import shlex
from typing import Any, Callable, Literal, TypeVar
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .bot_llm import BotLLMError, generate_bot_json, is_bot_llm_configured
from .config import get_settings
from .bot_copilot import handle_zalo_chat
from .models import (
    NotificationChannel,
    Shop,
    Task,
    TaskPriority,
    TaskStatus,
    TaskType,
    User,
    ZaloIncomingCommand,
)
from .notifications import enqueue_task_created_notifications, send_zalo_text
from .schemas import ZaloIncomingRequest
from .services import get_task_or_404, list_tasks, local_today, next_list_order

settings = get_settings()
T = TypeVar('T')


@dataclass(slots=True)
class ParsedZaloCommand:
    action: Literal['add', 'list', 'chat']
    title: str = ''
    assignee_token: str | None = None
    shop_token: str | None = None
    type_token: str | None = None
    due_token: str | None = None
    priority: TaskPriority = TaskPriority.medium
    view: Literal['today', 'inbox'] = 'today'


@dataclass(slots=True)
class IntentDecision:
    intent: Literal['create_task', 'list_tasks', 'chat', 'unknown']
    confidence: float
    reply_mode: Literal['execute', 'confirm', 'chat']
    reason: str = ''
    reply_message: str | None = None
    view: Literal['today', 'inbox'] | None = None


def _is_admin(user: User) -> bool:
    return (user.role or '').lower() == 'admin'


def _normalize_lookup(value: str) -> str:
    return re.sub(r'[\W_]+', '', value.casefold(), flags=re.UNICODE)


def _clean_token(value: str) -> str:
    return value.strip().strip('.,;:!?)(')


def _today() -> date:
    return datetime.now(ZoneInfo(settings.app_timezone)).date()


def _check_secret(secret: str | None) -> None:
    expected = settings.zalo_shared_secret
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='ZALO_SHARED_SECRET is not configured.',
        )
    if not secret or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid Zalo internal secret.')


def _conversation_type(payload: ZaloIncomingRequest) -> str:
    return (payload.conversation_type or '').strip().casefold()


def _is_direct_conversation(payload: ZaloIncomingRequest) -> bool:
    return _conversation_type(payload) in {'direct', 'private', 'user', 'dm'}


def _conversation_channel(payload: ZaloIncomingRequest) -> NotificationChannel | None:
    if _conversation_type(payload) == 'group':
        return NotificationChannel.group
    if _is_direct_conversation(payload):
        return NotificationChannel.user
    return None


def _reply_target_id(payload: ZaloIncomingRequest) -> str | None:
    channel = _conversation_channel(payload)
    if channel == NotificationChannel.group:
        return payload.conversation_id
    if channel == NotificationChannel.user:
        return payload.from_uid
    return None


def _is_allowed_conversation(payload: ZaloIncomingRequest) -> bool:
    if _is_direct_conversation(payload):
        return bool(payload.from_uid)
    if _conversation_type(payload) != 'group':
        return False
    allowed = settings.zalo_allowed_group_id_list
    if not allowed:
        return True
    return bool(payload.conversation_id and payload.conversation_id in allowed)


def _strip_bot_alias(text: str, *, allow_plain_text: bool) -> str | None:
    stripped = text.strip()
    for alias in settings.zalo_bot_alias_list:
        candidate = alias.strip()
        if not candidate:
            continue
        if stripped.casefold() == candidate.casefold():
            return ''
        prefix = f'{candidate} '
        if stripped.casefold().startswith(prefix.casefold()):
            return stripped[len(prefix):].strip()
    if allow_plain_text:
        return stripped
    return None


def _parse_date_token(token: str | None) -> date | None:
    if not token:
        return None

    raw = _clean_token(token).casefold()
    today = _today()
    if raw in {'today', 'homnay', 'hômnay', 'hn'}:
        return today
    if raw in {'tomorrow', 'mai', 'ngaymai', 'ngàymai'}:
        return today + timedelta(days=1)

    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass

    for fmt in ('%d/%m', '%d-%m'):
        try:
            parsed = datetime.strptime(raw, fmt)
            candidate = date(today.year, parsed.month, parsed.day)
            if candidate < today:
                candidate = date(today.year + 1, parsed.month, parsed.day)
            return candidate
        except ValueError:
            pass

    return None


def _strip_trailing_politeness(text: str) -> str:
    cleaned = re.sub(
        r'\b(nha|nhé|nhe|nho|nhờ|giup|giúp|voi|với|dum|dùm|di|đi|a|ạ|ha|hả)\b[\s.!?]*$',
        '',
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r'\s+', ' ', cleaned).strip(' ,.;:!?')


def _extract_natural_due_token(text: str) -> tuple[str | None, str]:
    patterns = [
        (r'\b(hôm nay|hom nay)\b', 'today'),
        (r'\b(ngày mai|ngay mai|mai)\b', 'tomorrow'),
        (r'\b(\d{4}-\d{2}-\d{2})\b', None),
        (r'\b(\d{1,2}/\d{1,2}(?:/\d{4})?)\b', None),
        (r'\b(\d{1,2}-\d{1,2}(?:-\d{4})?)\b', None),
    ]
    working = text
    for pattern, normalized in patterns:
        match = re.search(pattern, working, flags=re.IGNORECASE)
        if not match:
            continue
        due_token = normalized or match.group(1)
        working = f'{working[:match.start()]} {working[match.end():]}'
        return due_token, re.sub(r'\s+', ' ', working).strip()
    return None, text


def _parse_natural_add_command(body: str) -> ParsedZaloCommand | None:
    normalized = body.casefold()
    has_create_intent = any(
        phrase in normalized
        for phrase in (
            'thêm task',
            'them task',
            'tạo task',
            'tao task',
            'add task',
            'new task',
            'thêm việc',
            'them viec',
            'tạo việc',
            'tao viec',
            'thêm giúp',
            'them giup',
            'tạo giúp',
            'tao giup',
            'thêm cho em',
            'them cho em',
            'thêm cho anh',
            'them cho anh',
            'thêm cho mình',
            'them cho minh',
            'nhớ thêm',
            'nho them',
        )
    )
    if not has_create_intent:
        return None

    try:
        tokens = shlex.split(body)
    except ValueError:
        tokens = body.split()

    parsed = ParsedZaloCommand(action='add')
    title_tokens: list[str] = []
    filler_words = {
        'thêm',
        'them',
        'tạo',
        'tao',
        'task',
        'việc',
        'viec',
        'giúp',
        'giup',
        'cho',
        'em',
        'anh',
        'chị',
        'chi',
        'mình',
        'minh',
        'nhớ',
        'nho',
        'new',
        'add',
        'giùm',
        'dum',
    }
    skip_next_due_word = False
    for token in tokens:
        lower = token.casefold()
        if lower.startswith('due:') or lower.startswith('date:'):
            parsed.due_token = token.split(':', 1)[1]
            continue
        if lower.startswith('type:'):
            parsed.type_token = token.split(':', 1)[1]
            continue
        if lower.startswith('!'):
            priority_token = _clean_token(lower.removeprefix('!'))
            if priority_token in {'low', 'medium', 'high'}:
                parsed.priority = TaskPriority(priority_token)
                continue
        if token.startswith('@') and not parsed.assignee_token:
            parsed.assignee_token = token
            continue
        if token.startswith('#') and not parsed.shop_token:
            parsed.shop_token = token
            continue
        if lower in {'hôm', 'hom'}:
            skip_next_due_word = True
            parsed.due_token = parsed.due_token or 'today'
            continue
        if skip_next_due_word and lower == 'nay':
            skip_next_due_word = False
            continue
        if lower in {'ngày', 'ngay'}:
            skip_next_due_word = True
            parsed.due_token = parsed.due_token or 'tomorrow'
            continue
        if skip_next_due_word and lower == 'mai':
            skip_next_due_word = False
            continue
        if lower == 'mai':
            parsed.due_token = parsed.due_token or 'tomorrow'
            continue
        if lower in filler_words:
            continue
        title_tokens.append(token)

    title = ' '.join(title_tokens).strip()
    if not parsed.due_token:
        parsed.due_token, title = _extract_natural_due_token(title)

    title = re.sub(
        r'^(làm|lam|việc|viec|task)\s+',
        '',
        title,
        flags=re.IGNORECASE,
    )
    title = _strip_trailing_politeness(title)
    parsed.title = title
    if not parsed.title:
        return None
    return parsed


def _resolve_unique_by_token(items: list[T], token: str, name_getter: Callable[[T], list[str]]) -> T | None:
    normalized = _normalize_lookup(token)
    if not normalized:
        return None

    exact: list[T] = []
    prefix: list[T] = []
    for item in items:
        names = [str(value or '') for value in name_getter(item)]
        keys = [_normalize_lookup(value) for value in names if value]
        if normalized in keys:
            exact.append(item)
        elif any(key.startswith(normalized) for key in keys):
            prefix.append(item)

    if len(exact) == 1:
        return exact[0]
    if not exact and len(prefix) == 1:
        return prefix[0]
    return None


def _resolve_actor(db: Session, from_uid: str | None) -> User | None:
    uid = (from_uid or '').strip()
    if not uid:
        return None
    return db.scalar(select(User).where(User.zalo_user_id == uid, User.is_active.is_(True)))


def _resolve_user(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    clean = _clean_token(token.removeprefix('@'))
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    return _resolve_unique_by_token(users, clean, lambda user: [user.username, user.name, user.zalo_user_id])


def _resolve_shop(db: Session, token: str | None) -> Shop | None:
    if not token:
        return None
    clean = _clean_token(token.removeprefix('#'))
    shops = db.scalars(select(Shop)).all()
    return _resolve_unique_by_token(shops, clean, lambda shop: [shop.name])


def _resolve_task_type(db: Session, token: str | None) -> TaskType | None:
    if not token:
        return None
    task_types = db.scalars(select(TaskType)).all()
    return _resolve_unique_by_token(task_types, _clean_token(token), lambda task_type: [task_type.name])


def _auto_prefix_title_for_type(title: str, task_type: TaskType | None) -> str:
    if not task_type:
        return title
    prefix = f'[{task_type.name.strip()}]'
    if title.casefold().startswith(prefix.casefold()):
        return title
    return f'{prefix} {title}'


def _message_key(payload: ZaloIncomingRequest) -> str:
    if payload.message_id:
        return f'{payload.conversation_id or ""}:{payload.message_id}'

    digest = hashlib.sha256(
        f'{payload.conversation_id or ""}|{payload.from_uid or ""}|{payload.text}'.encode('utf-8')
    ).hexdigest()[:24]
    return f'no-id:{digest}:{datetime.utcnow().timestamp()}'


def _parse_command(text: str, *, allow_plain_text: bool) -> ParsedZaloCommand | None:
    body = _strip_bot_alias(text, allow_plain_text=allow_plain_text)
    if body is None:
        return None

    try:
        tokens = shlex.split(body)
    except ValueError:
        tokens = body.split()

    if not tokens:
        return None

    action = tokens[0].casefold()
    if action in {'list', 'ls'}:
        view = tokens[1].casefold() if len(tokens) > 1 else 'today'
        if view not in {'today', 'inbox'}:
            view = 'today'
        return ParsedZaloCommand(action='list', view=view)  # type: ignore[arg-type]

    if action not in {'add', 'task', 'new'}:
        natural_add = _parse_natural_add_command(body)
        if natural_add is not None:
            return natural_add
        return ParsedZaloCommand(action='chat', title=body)

    title_tokens: list[str] = []
    parsed = ParsedZaloCommand(action='add')
    for token in tokens[1:]:
        lower = token.casefold()
        if lower.startswith('due:') or lower.startswith('date:'):
            parsed.due_token = token.split(':', 1)[1]
            continue
        if lower.startswith('type:'):
            parsed.type_token = token.split(':', 1)[1]
            continue
        if lower.startswith('!'):
            priority_token = _clean_token(lower.removeprefix('!'))
            if priority_token in {'low', 'medium', 'high'}:
                parsed.priority = TaskPriority(priority_token)
                continue
        if token.startswith('@') and not parsed.assignee_token:
            parsed.assignee_token = token
            continue
        if token.startswith('#') and not parsed.shop_token:
            parsed.shop_token = token
            continue
        title_tokens.append(token)

    parsed.title = ' '.join(title_tokens).strip()
    return parsed


def _llm_intent_system_prompt() -> str:
    return (
        'You classify a Zalo office message into one intent. '
        'Return strict JSON only. '
        'Valid intents: create_task, list_tasks, chat, unknown. '
        'Valid reply_mode: execute, confirm, chat. '
        'Use create_task only when the user is actually asking to create/add a task, not when they are merely discussing or reminding. '
        'Use list_tasks when they ask to see current tasks. '
        'Use execute only when the request is explicit and safe to run without confirmation. '
        'Use confirm when it sounds like task creation might be intended but is still ambiguous. '
        'Use chat for general conversation, discussion, planning, reminders, or unclear cases.'
    )


def _llm_intent_user_prompt(*, actor: User, text: str) -> str:
    return (
        f'Actor name: {actor.name}\n'
        f'Actor username: {actor.username}\n'
        f'Actor role: {actor.role or "unknown"}\n'
        f'Message: {text}\n\n'
        'Return JSON object with keys:\n'
        '- intent\n'
        '- confidence (0..1)\n'
        '- reply_mode\n'
        '- reason\n'
        '- reply_message\n'
        '- view (today|inbox|null)\n'
    )


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _llm_intent_decision(*, actor: User, text: str) -> IntentDecision | None:
    if not is_bot_llm_configured():
        return None
    try:
        payload = generate_bot_json(
            system_prompt=_llm_intent_system_prompt(),
            user_prompt=_llm_intent_user_prompt(actor=actor, text=text),
        )
    except BotLLMError:
        return None

    intent = str(payload.get('intent') or 'unknown').strip().lower()
    if intent not in {'create_task', 'list_tasks', 'chat', 'unknown'}:
        intent = 'unknown'
    reply_mode = str(payload.get('reply_mode') or 'chat').strip().lower()
    if reply_mode not in {'execute', 'confirm', 'chat'}:
        reply_mode = 'chat'
    view = str(payload.get('view') or '').strip().lower() or None
    if view not in {'today', 'inbox', None}:
        view = None
    return IntentDecision(
        intent=intent,  # type: ignore[arg-type]
        confidence=_coerce_confidence(payload.get('confidence')),
        reply_mode=reply_mode,  # type: ignore[arg-type]
        reason=str(payload.get('reason') or '').strip(),
        reply_message=str(payload.get('reply_message') or '').strip() or None,
        view=view,  # type: ignore[arg-type]
    )


def _llm_extract_system_prompt() -> str:
    return (
        'Extract a task payload from a Zalo office message. Return strict JSON only. '
        'Do not invent missing data. '
        'Priority must be one of low, medium, high. '
        'Prefer due_token values like today, tomorrow, dd/mm, dd/mm/yyyy, yyyy-mm-dd when present.'
    )


def _llm_extract_user_prompt(
    *,
    actor: User,
    text: str,
    users: list[User],
    shops: list[Shop],
    task_types: list[TaskType],
) -> str:
    user_lines = '\n'.join(f'- {user.name} | username={user.username} | zalo={user.zalo_user_id}' for user in users)
    shop_lines = '\n'.join(f'- {shop.name}' for shop in shops) or '- none'
    type_lines = '\n'.join(f'- {task_type.name}' for task_type in task_types) or '- none'
    return (
        f'Actor:\n- {actor.name} | username={actor.username} | role={actor.role or "unknown"}\n\n'
        f'Available users:\n{user_lines or "- none"}\n\n'
        f'Available shops:\n{shop_lines}\n\n'
        f'Available task types:\n{type_lines}\n\n'
        f'Message:\n{text}\n\n'
        'Return JSON object with keys:\n'
        '- title\n'
        '- assignee_token\n'
        '- shop_token\n'
        '- type_token\n'
        '- due_token\n'
        '- priority\n'
        '- needs_confirmation\n'
        '- confirmation_message\n'
    )


def _llm_extract_add_command(db: Session, *, actor: User, text: str) -> ParsedZaloCommand | tuple[str, None] | None:
    if not is_bot_llm_configured():
        return None
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    shops = db.scalars(select(Shop)).all()
    task_types = db.scalars(select(TaskType)).all()
    try:
        payload = generate_bot_json(
            system_prompt=_llm_extract_system_prompt(),
            user_prompt=_llm_extract_user_prompt(
                actor=actor,
                text=text,
                users=users,
                shops=shops,
                task_types=task_types,
            ),
        )
    except BotLLMError:
        return None

    title = str(payload.get('title') or '').strip()
    if not title:
        return None
    confirmation_message = str(payload.get('confirmation_message') or '').strip() or None
    needs_confirmation = bool(payload.get('needs_confirmation'))
    priority_token = str(payload.get('priority') or 'medium').strip().lower()
    if priority_token not in {'low', 'medium', 'high'}:
        priority_token = 'medium'
    parsed = ParsedZaloCommand(
        action='add',
        title=title,
        assignee_token=str(payload.get('assignee_token') or '').strip() or None,
        shop_token=str(payload.get('shop_token') or '').strip() or None,
        type_token=str(payload.get('type_token') or '').strip() or None,
        due_token=str(payload.get('due_token') or '').strip() or None,
        priority=TaskPriority(priority_token),
    )
    if needs_confirmation:
        return (
            confirmation_message
            or f'Tôi hiểu là bạn muốn tạo task "{title}", nhưng câu này còn hơi mơ hồ. Xác nhận lại giúp tôi nhé.',
            None,
        )
    return parsed


def _reply_to_conversation(
    *,
    channel: NotificationChannel | None,
    target_id: str | None,
    message: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    if channel is None:
        return {'ok': False, 'error': 'unsupported_conversation_type'}
    if not target_id:
        return {'ok': False, 'error': 'missing_target_id'}

    ok, status_code, body, error = send_zalo_text(
        channel=channel,
        target_id=target_id,
        message=message,
        context=context,
    )
    return {
        'ok': ok,
        'status_code': status_code,
        'body': body,
        'error': error,
    }


def _usage_message() -> str:
    alias = settings.zalo_bot_alias_list[0]
    return (
        'Mình chưa hiểu lệnh này. Dùng ví dụ:\n'
        f'{alias} add Fix mockup @quang #AmzMage type:Design due:tomorrow !high\n'
        f'{alias} list today'
    )


def _create_zalo_task(db: Session, parsed: ParsedZaloCommand, actor: User) -> Task:
    if not parsed.title:
        raise ValueError(_usage_message())

    assignee = _resolve_user(db, parsed.assignee_token) if parsed.assignee_token else actor
    if parsed.assignee_token and not assignee:
        raise ValueError(f'Không tìm thấy assignee {parsed.assignee_token}.')

    if not _is_admin(actor) and assignee.id != actor.id:
        raise PermissionError('Member chỉ được tạo task cho chính mình.')

    shop = _resolve_shop(db, parsed.shop_token)
    if parsed.shop_token and not shop:
        raise ValueError(f'Không tìm thấy shop {parsed.shop_token}.')

    task_type = _resolve_task_type(db, parsed.type_token)
    if parsed.type_token and not task_type:
        raise ValueError(f'Không tìm thấy task type {parsed.type_token}.')

    due_date = _parse_date_token(parsed.due_token)
    if parsed.due_token and not due_date:
        raise ValueError(f'Không hiểu due date "{parsed.due_token}".')

    title = _auto_prefix_title_for_type(parsed.title, task_type)
    task = Task(
        title=title,
        status=TaskStatus.todo,
        assigned_to=assignee.id,
        created_by=actor.id,
        shop_id=shop.id if shop else None,
        type_id=task_type.id if task_type else None,
        due_date=due_date,
        priority=parsed.priority,
        is_someday=False,
        list_order=next_list_order(db),
    )
    db.add(task)
    db.flush()
    return task


def _task_url(task_id: int) -> str | None:
    base = (settings.task_public_base_url or '').strip().rstrip('/')
    if not base:
        return None
    if '{task_id}' in base:
        return base.replace('{task_id}', str(task_id))
    if base.endswith('/task') or '/task/' in base:
        return base
    return f'{base}/task'


def _format_task_created(task: Task) -> str:
    assignee = task.assignee.name if task.assignee else task.assigned_to or 'Unassigned'
    parts = [f'Đã tạo task #{task.id}: {task.title}', f'Assignee: {assignee}']
    if task.due_date:
        parts.append(f'Due: {task.due_date.strftime("%d/%m/%Y")}')
    url = _task_url(task.id)
    if url:
        parts.append(url)
    return '\n'.join(parts)


def _format_task_line(task: Any) -> str:
    due = f' - due {task.due_date.strftime("%d/%m")}' if task.due_date else ''
    return f'• #{task.id} {task.title}{due} [{task.status.value}]'


def _format_list_response(view: str, groups) -> str:
    lines = [f'Task {view} của bạn:']
    count = 0
    for group in groups:
        if count >= 10:
            break
        lines.append(f'\n{group.title}:')
        for task in group.tasks:
            if count >= 10:
                break
            lines.append(_format_task_line(task))
            count += 1

    if count == 0:
        return f'Không có task {view} nào.'
    return '\n'.join(lines)


def _handle_add_and_reply(
    *,
    db: Session,
    parsed: ParsedZaloCommand,
    actor: User,
    channel: NotificationChannel | None,
    target_id: str | None,
    record: ZaloIncomingCommand,
) -> dict[str, Any]:
    task = _create_zalo_task(db, parsed, actor)
    db.commit()
    full_task = get_task_or_404(db, task.id) or task
    try:
        enqueue_task_created_notifications(db, full_task)
    except Exception:
        db.rollback()
    message = _format_task_created(full_task)
    reply = _reply_to_conversation(
        channel=channel,
        target_id=target_id,
        message=message,
        context={'source': 'zalo_command', 'command': 'add', 'task_id': task.id},
    )
    record.task_id = task.id
    record.command = 'add'
    record.response_payload = {'message': message, 'reply': reply}
    db.commit()
    return {'ok': True, 'action': 'add', 'task_id': task.id, 'reply': reply}


def _handle_list_and_reply(
    *,
    db: Session,
    view: Literal['today', 'inbox'],
    actor: User,
    channel: NotificationChannel | None,
    target_id: str | None,
    record: ZaloIncomingCommand,
) -> dict[str, Any]:
    result = list_tasks(
        db,
        view,
        actor_id=actor.id,
        actor_is_admin=_is_admin(actor),
    )
    message = _format_list_response(view, result.groups)
    reply = _reply_to_conversation(
        channel=channel,
        target_id=target_id,
        message=message,
        context={'source': 'zalo_command', 'command': 'list', 'view': view},
    )
    record.command = 'list'
    record.response_payload = {'message': message, 'reply': reply}
    db.commit()
    return {'ok': True, 'action': 'list', 'view': view, 'reply': reply}


def _persist_command(
    db: Session,
    *,
    payload: ZaloIncomingRequest,
    command: str,
) -> tuple[ZaloIncomingCommand, bool]:
    key = _message_key(payload)
    existing = db.scalar(select(ZaloIncomingCommand).where(ZaloIncomingCommand.message_key == key))
    if existing:
        return existing, False

    record = ZaloIncomingCommand(
        message_key=key,
        message_id=payload.message_id,
        conversation_id=payload.conversation_id,
        conversation_type=payload.conversation_type,
        from_uid=payload.from_uid,
        text=payload.text,
        command=command,
        response_payload={},
    )
    try:
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(ZaloIncomingCommand).where(ZaloIncomingCommand.message_key == key))
        if existing:
            return existing, False
        raise
    return record, True


def handle_zalo_incoming(
    *,
    db: Session,
    payload: ZaloIncomingRequest,
    x_internal_secret: str | None,
) -> dict[str, Any]:
    _check_secret(x_internal_secret)

    if not _is_allowed_conversation(payload):
        return {'ok': True, 'ignored': True, 'reason': 'conversation_not_allowed'}

    channel = _conversation_channel(payload)
    target_id = _reply_target_id(payload)
    parsed = _parse_command(payload.text, allow_plain_text=_is_direct_conversation(payload))
    if parsed is None:
        return {'ok': True, 'ignored': True, 'reason': 'missing_bot_alias'}

    record, inserted = _persist_command(db, payload=payload, command=parsed.action)
    if not inserted:
        return {'ok': True, 'duplicate': True, 'id': record.id, 'response': record.response_payload}
    db.commit()

    actor = _resolve_actor(db, payload.from_uid)
    if not actor:
        message = 'Bạn chưa được liên kết user Task Manager. Nhắn admin cập nhật zalo_user_id giúp nha.'
        reply = _reply_to_conversation(
            channel=channel,
            target_id=target_id,
            message=message,
            context={'source': 'zalo_command', 'command': parsed.action, 'message_id': payload.message_id},
        )
        record.response_payload = {'message': message, 'reply': reply}
        db.commit()
        return {'ok': False, 'error': 'unmapped_sender', 'reply': reply}

    try:
        if parsed.action == 'chat':
            decision = _llm_intent_decision(actor=actor, text=parsed.title or payload.text)
            if decision and decision.intent == 'create_task':
                if decision.reply_mode == 'confirm':
                    message = (
                        decision.reply_message
                        or 'Tôi hiểu là bạn có thể muốn tạo task mới, nhưng câu này còn hơi mơ hồ. Xác nhận rõ hơn giúp tôi.'
                    )
                    reply = _reply_to_conversation(
                        channel=channel,
                        target_id=target_id,
                        message=message,
                        context={'source': 'zalo_command', 'command': 'confirm_add', 'message_id': payload.message_id},
                    )
                    record.command = 'confirm_add'
                    record.response_payload = {'message': message, 'reply': reply, 'reason': decision.reason}
                    db.commit()
                    return {'ok': True, 'action': 'confirm', 'reply': reply}

                if decision.reply_mode == 'execute' and decision.confidence >= 0.88:
                    extracted = _llm_extract_add_command(db, actor=actor, text=parsed.title or payload.text)
                    if isinstance(extracted, tuple):
                        message, _ = extracted
                        reply = _reply_to_conversation(
                            channel=channel,
                            target_id=target_id,
                            message=message,
                            context={'source': 'zalo_command', 'command': 'confirm_add', 'message_id': payload.message_id},
                        )
                        record.command = 'confirm_add'
                        record.response_payload = {'message': message, 'reply': reply, 'reason': decision.reason}
                        db.commit()
                        return {'ok': True, 'action': 'confirm', 'reply': reply}
                    if extracted is not None:
                        return _handle_add_and_reply(
                            db=db,
                            parsed=extracted,
                            actor=actor,
                            channel=channel,
                            target_id=target_id,
                            record=record,
                        )

            if decision and decision.intent == 'list_tasks':
                view = decision.view or 'today'
                return _handle_list_and_reply(
                    db=db,
                    view=view,
                    actor=actor,
                    channel=channel,
                    target_id=target_id,
                    record=record,
                )

        if parsed.action == 'chat':
            chat = handle_zalo_chat(
                db=db,
                actor=actor,
                incoming_text=parsed.title or payload.text,
                conversation_id=payload.conversation_id or payload.from_uid,
                message_id=payload.message_id,
                reply_channel=channel,
                reply_target_id=target_id,
            )
            record.response_payload = {'message': chat.message, 'reply': chat.reply, 'used_llm': chat.used_llm}
            db.commit()
            return {'ok': True, 'action': 'chat', 'used_llm': chat.used_llm, 'reply': chat.reply}

        if parsed.action == 'add':
            return _handle_add_and_reply(
                db=db,
                parsed=parsed,
                actor=actor,
                channel=channel,
                target_id=target_id,
                record=record,
            )

        return _handle_list_and_reply(
            db=db,
            view=parsed.view,
            actor=actor,
            channel=channel,
            target_id=target_id,
            record=record,
        )
    except PermissionError as exc:
        db.rollback()
        message = str(exc)
    except ValueError as exc:
        db.rollback()
        message = str(exc)

    reply = _reply_to_conversation(
        channel=channel,
        target_id=target_id,
        message=message,
        context={'source': 'zalo_command', 'command': parsed.action, 'message_id': payload.message_id},
    )
    record.response_payload = {'message': message, 'reply': reply}
    db.add(record)
    db.commit()
    return {'ok': False, 'error': 'command_failed', 'message': message, 'reply': reply}
