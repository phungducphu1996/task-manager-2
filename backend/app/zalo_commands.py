from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import hmac
import json
import re
import shlex
import unicodedata
from typing import Any, Callable, Literal, TypeVar
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, object_session

from .bot_files import (
    contact_prompt_text_for_group,
    contact_prompt_text_for_user,
    contact_registry_text,
    persona_text,
    profile_summary_text,
    profile_text,
    user_contact_aliases,
)
from .bot_llm import BotLLMError, complete_bot_conversation, is_bot_llm_configured
from .bot_memory import (
    conversation_state_text,
    get_conversation_state,
    recent_conversation_text,
    recent_memory_text,
    store_conversation_message,
    upsert_conversation_state,
)
from .config import get_settings
from .bot_copilot import handle_zalo_chat
from .models import (
    NotificationChannel,
    ReminderRule,
    ReminderRuleType,
    ReminderScheduleType,
    Shop,
    Task,
    TaskAttachment,
    TaskPriority,
    TaskStatus,
    TaskType,
    User,
    VikunjaTaskMapping,
    VikunjaUserMapping,
    ZaloIncomingCommand,
)
from .notifications import (
    enqueue_task_created_notifications,
    enqueue_task_status_transition_notifications,
    enqueue_task_updated_notifications,
    send_zalo_text,
)
from .reminders import (
    create_reminder_rule,
    create_task_nudge_rule,
    handle_reminder_interaction,
    update_reminder_rule,
)
from .schemas import ZaloIncomingRequest
from .services import get_task_or_404, list_tasks, local_today, next_list_order
from .vikunja import (
    VikunjaConfigError,
    build_vikunja_task_status_map,
    ensure_vikunja_project,
    get_vikunja_client,
    vikunja_task_status_from_payload,
)

settings = get_settings()
T = TypeVar('T')
MEMBER_ALLOWED_STATUSES = {TaskStatus.todo, TaskStatus.doing, TaskStatus.review}
GENERIC_NAME_PARTS = {'anh', 'chi', 'chị', 'em', 'ban', 'bạn', 'minh', 'mình'}
VIKUNJA_PRIORITY_TO_TASK_PRIORITY = {
    1: TaskPriority.low.value,
    2: TaskPriority.low.value,
    3: TaskPriority.medium.value,
    4: TaskPriority.high.value,
    5: TaskPriority.high.value,
}
TASK_PRIORITY_TO_VIKUNJA_PRIORITY = {
    TaskPriority.low.value: 2,
    TaskPriority.medium.value: 3,
    TaskPriority.high.value: 4,
}


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

def _is_admin(user: User) -> bool:
    return (user.role or '').lower() == 'admin'


def _normalize_lookup(value: str) -> str:
    folded = value.casefold().replace('đ', 'd')
    ascii_text = unicodedata.normalize('NFKD', folded).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[\W_]+', '', ascii_text, flags=re.UNICODE)


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


def _mention_values(mention: dict) -> list[str]:
    values: list[str] = []
    for key in ('uid', 'id', 'user_id', 'userId', 'zalo_user_id', 'zaloUserId', 'label', 'name', 'displayName'):
        value = mention.get(key)
        if value is not None:
            values.append(str(value).strip())
    return [value for value in values if value]


def _is_bot_mentioned(payload: ZaloIncomingRequest) -> bool:
    if not payload.mentions:
        return False

    configured_ids = {item.casefold() for item in settings.zalo_bot_user_id_list}
    aliases = {alias.strip().lstrip('@').casefold() for alias in settings.zalo_bot_alias_list}
    for mention in payload.mentions:
        values = _mention_values(mention)
        folded = {value.casefold() for value in values}
        if configured_ids and folded & configured_ids:
            return True
        labels = {value.lstrip('@').casefold() for value in values}
        if aliases & labels:
            return True
    return False


def _strip_leading_mention(text: str, payload: ZaloIncomingRequest) -> str:
    stripped = text.strip()
    for mention in payload.mentions:
        for value in _mention_values(mention):
            label = value.strip()
            if not label:
                continue
            candidates = {label, f'@{label.lstrip("@")}'}
            for candidate in candidates:
                prefix = f'{candidate} '
                if stripped.casefold().startswith(prefix.casefold()):
                    return stripped[len(prefix):].strip()
                if stripped.casefold() == candidate.casefold():
                    return ''
    return stripped


def _message_body_for_bot(payload: ZaloIncomingRequest) -> str | None:
    alias_body = _strip_bot_alias(payload.text, allow_plain_text=_is_direct_conversation(payload))
    if alias_body is not None:
        return alias_body
    if _conversation_type(payload) == 'group' and _is_bot_mentioned(payload):
        return _strip_leading_mention(payload.text, payload)
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
    if raw in {'dayaftertomorrow', 'mot', 'mốt', 'ngaymot', 'ngàymốt', 'ngaymốt'}:
        return today + timedelta(days=2)

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
        (r'\b(ngày mốt|ngay mot|mốt|mot)\b', 'dayaftertomorrow'),
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
    return _resolve_unique_by_token(
        users,
        clean,
        lambda user: [user.id, user.username, user.name, user.zalo_user_id, *user_contact_aliases(user)],
    )


def _matching_users_for_token(db: Session, token: str | None) -> list[User]:
    clean = _clean_token((token or '').removeprefix('@'))
    normalized = _normalize_lookup(clean)
    if not normalized:
        return []

    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    matches: list[User] = []
    for user in users:
        names = [user.id, user.username, user.name, user.zalo_user_id, *user_contact_aliases(user)]
        keys = [_normalize_lookup(str(value or '')) for value in names if value]
        if normalized in keys or any(key.startswith(normalized) for key in keys):
            matches.append(user)
    return matches


def _user_is_mentioned_in_text(user: User, text: str | None) -> bool:
    normalized_text = _normalize_lookup(text or '')
    if not normalized_text:
        return False

    direct_values = [user.id, user.username, user.name, user.zalo_user_id, *user_contact_aliases(user)]
    for value in direct_values:
        normalized_value = _normalize_lookup(str(value or ''))
        if normalized_value and normalized_value in normalized_text:
            return True

    for part in re.split(r'\s+', user.name or ''):
        normalized_part = _normalize_lookup(part)
        if normalized_part in {_normalize_lookup(item) for item in GENERIC_NAME_PARTS}:
            continue
        if len(normalized_part) >= 3 and normalized_part in normalized_text:
            return True
    return False


def _mentioned_users_in_text(db: Session, text: str | None) -> list[User]:
    if not text:
        return []
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    return [user for user in users if _user_is_mentioned_in_text(user, text)]


def _looks_like_assignee_update(text: str | None) -> bool:
    normalized = _normalize_lookup(text or '')
    if not normalized:
        return False
    return any(
        token in normalized
        for token in (
            'chuyenquacho',
            'doiqua',
            'doisang',
            'gancho',
            'assigncho',
            'giacho',
            'chochi',
            'choanh',
            'choban',
            'cho',
        )
    )


def _explicit_due_token_from_text(text: str | None) -> str | None:
    if not text:
        return None
    due_token, _ = _extract_natural_due_token(text)
    return due_token


def _state_data_from_tool_results(*, existing_state: dict, tool_results: list[dict[str, Any]]) -> dict:
    next_state = dict(existing_state or {})
    successful: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for item in tool_results:
        name = str(item.get('name') or 'tool')
        result = item.get('result') or {}
        if result.get('ok'):
            successful.append({'name': name, 'task': result.get('task'), 'changed_fields': result.get('changed_fields')})
            task = result.get('task')
            if not task and name == 'find_tasks':
                tasks = result.get('tasks') or []
                if len(tasks) == 1:
                    task = tasks[0]
            if isinstance(task, dict):
                next_state['active_task'] = task
                fields: dict[str, Any] = {}
                if name == 'update_task_status' and task.get('status'):
                    fields['status'] = task.get('status')
                for field in result.get('changed_fields') or []:
                    if field == 'assigned_to':
                        fields['assignee'] = task.get('assignee')
                    elif field == 'due_date':
                        fields['due_date'] = task.get('due_date')
                    elif field == 'status':
                        fields['status'] = task.get('status')
                    else:
                        fields[field] = True
                if fields:
                    next_state['pending_intent'] = {
                        'action': 'update_task',
                        'task_id': task.get('id'),
                        'fields': fields,
                        'needs_confirmation': False,
                    }
        else:
            failed.append({'name': name, 'error': result.get('error') or 'Tool failed.'})

    next_state['last_successful_tool_results'] = successful[-5:]
    next_state['last_failed_tool_results'] = failed[-5:]
    return next_state


def _ground_update_arguments(
    db: Session,
    *,
    arguments: dict[str, Any],
    current_text: str | None,
    state_data: dict | None,
) -> dict[str, Any]:
    grounded = dict(arguments)

    if not str(grounded.get('assignee_token') or '').strip() and _looks_like_assignee_update(current_text):
        mentioned_users = _mentioned_users_in_text(db, current_text)
        if len(mentioned_users) == 1:
            grounded['assignee_token'] = mentioned_users[0].username or mentioned_users[0].id

    if not str(grounded.get('assignee_token') or '').strip():
        pending = (state_data or {}).get('pending_intent') or {}
        fields = pending.get('fields') or {}
        assignee_token = fields.get('assignee_token') or fields.get('assignee')
        if assignee_token:
            resolved = _resolve_user(db, str(assignee_token))
            if resolved:
                grounded['assignee_token'] = resolved.username or resolved.id

    explicit_due = _explicit_due_token_from_text(current_text)
    if explicit_due:
        grounded['due_token'] = explicit_due

    return grounded


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


def _replace_type_prefix_for_title(db: Session, title: str, task_type: TaskType | None) -> str:
    clean_title = title.strip()
    for existing_type in db.scalars(select(TaskType)).all():
        prefix = f'[{existing_type.name.strip()}]'
        if clean_title.casefold().startswith(prefix.casefold()):
            clean_title = clean_title[len(prefix):].strip()
            break
    return _auto_prefix_title_for_type(clean_title, task_type)


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


def _parse_command_body(body: str) -> ParsedZaloCommand | None:
    try:
        tokens = shlex.split(body)
    except ValueError:
        tokens = body.split()

    if not tokens:
        return ParsedZaloCommand(action='chat', title='')

    action = tokens[0].casefold()
    if action in {'list', 'ls'}:
        view = tokens[1].casefold() if len(tokens) > 1 else 'today'
        if view not in {'today', 'inbox'}:
            view = 'today'
        return ParsedZaloCommand(action='list', view=view)  # type: ignore[arg-type]

    if action not in {'add', 'task', 'new'}:
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


def _tool_specs() -> list[dict[str, Any]]:
    return [
        {
            'type': 'function',
            'function': {
                'name': 'find_tasks',
                'description': 'Find tasks by title, id, assignee, or review status before taking action.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'query': {'type': 'string'},
                        'status': {'type': 'string', 'enum': ['todo', 'doing', 'review', 'ready', 'done']},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10},
                    },
                    'required': ['query'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'list_tasks',
                'description': 'List tasks for the current actor in a known view.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'view': {'type': 'string', 'enum': ['today', 'inbox', 'review', 'logbook']},
                    },
                    'required': ['view'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'create_task',
                'description': 'Create a new task for the actor or an allowed assignee.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'title': {'type': 'string'},
                        'assignee_token': {'type': 'string'},
                        'shop_token': {'type': 'string'},
                        'type_token': {'type': 'string'},
                        'due_token': {'type': 'string'},
                        'priority': {'type': 'string', 'enum': ['low', 'medium', 'high']},
                    },
                    'required': ['title'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'approve_task',
                'description': 'Approve a review task by moving it from review to ready. Only admins may do this.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'task_id': {'type': 'integer'},
                    },
                    'required': ['task_id'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'update_task_status',
                'description': (
                    'Move a task to todo, doing, review, ready, or done. Use find_tasks first when the task is not '
                    'identified by id. Backend enforces member/admin status rules.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'task_id': {'type': 'integer'},
                        'status': {'type': 'string', 'enum': ['todo', 'doing', 'review', 'ready', 'done']},
                    },
                    'required': ['task_id', 'status'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'update_task_fields',
                'description': (
                    'Edit existing task fields after finding the correct task. Supports title, description, assignee, '
                    'shop, type, due date/deadline, priority, notes, status, and adding one link attachment. Use '
                    'find_tasks first when the task is not identified by id.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'task_id': {'type': 'integer'},
                        'title': {'type': 'string'},
                        'description': {'type': 'string'},
                        'assignee_token': {'type': 'string'},
                        'clear_assignee': {'type': 'boolean'},
                        'shop_token': {'type': 'string'},
                        'clear_shop': {'type': 'boolean'},
                        'type_token': {'type': 'string'},
                        'clear_type': {'type': 'boolean'},
                        'due_token': {
                            'type': 'string',
                            'description': 'today, tomorrow, dd/mm, dd/mm/yyyy, yyyy-mm-dd, or empty when unchanged.',
                        },
                        'clear_due_date': {'type': 'boolean'},
                        'priority': {'type': 'string', 'enum': ['low', 'medium', 'high']},
                        'status': {'type': 'string', 'enum': ['todo', 'doing', 'review', 'ready', 'done']},
                        'notes': {'type': 'string'},
                        'link_url': {'type': 'string'},
                        'link_name': {'type': 'string'},
                    },
                    'required': ['task_id'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'send_message',
                'description': (
                    'Send a Zalo message to a specific user or group when the current actor asks the bot to relay a '
                    'message. Only send to a user that appears in the active user directory and is explicitly named '
                    'in the latest user message. If the requested recipient is missing from the directory, do not '
                    'guess; ask the actor to add that person first.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'channel': {'type': 'string', 'enum': ['user', 'group']},
                        'target_token': {
                            'type': 'string',
                            'description': 'User id, username, name, zalo_user_id, "current_group", or "default_group".',
                        },
                        'message': {'type': 'string'},
                    },
                    'required': ['channel', 'target_token', 'message'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'create_reminder_rule',
                'description': (
                    'Create a reminder rule. Use this when the actor asks to remind, nudge, check in, send daily '
                    'digests, or run daily strategy suggestions. Use find_tasks first for task_nudge if task is not '
                    'identified by id.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'name': {'type': 'string'},
                        'rule_type': {
                            'type': 'string',
                            'enum': ['daily_group_digest', 'daily_member_checkin', 'task_nudge', 'daily_strategy'],
                        },
                        'target_channel': {'type': 'string', 'enum': ['user', 'group']},
                        'target_token': {'type': 'string'},
                        'task_id': {'type': 'integer'},
                        'schedule_time': {'type': 'string', 'description': 'HH:MM for daily reminders.'},
                        'interval_minutes': {'type': 'integer'},
                        'max_runs_per_day': {'type': 'integer'},
                    },
                    'required': ['name', 'rule_type'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'list_reminder_rules',
                'description': 'List active reminder rules visible to the actor.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'include_disabled': {'type': 'boolean'},
                    },
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'disable_reminder_rule',
                'description': 'Disable an existing reminder rule by id.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'rule_id': {'type': 'integer'},
                    },
                    'required': ['rule_id'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'update_reminder_rule',
                'description': 'Update an existing reminder rule by id.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'rule_id': {'type': 'integer'},
                        'name': {'type': 'string'},
                        'enabled': {'type': 'boolean'},
                        'schedule_time': {'type': 'string'},
                        'interval_minutes': {'type': 'integer'},
                        'max_runs_per_day': {'type': 'integer'},
                    },
                    'required': ['rule_id'],
                    'additionalProperties': False,
                },
            },
        },
    ]


def _active_user_directory_text(db: Session) -> str:
    users = db.scalars(
        select(User).where(User.is_active.is_(True)).order_by(User.full_name.asc(), User.username.asc())
    ).all()
    if not users:
        return 'Không có user active nào.'

    lines: list[str] = ['# Active User Directory']
    for user in users:
        lines.append(
            '- '
            + ' | '.join(
                [
                    f'name={user.name}',
                    f'username={user.username}',
                    f'role={user.role or "unknown"}',
                    f'zalo={user.zalo_user_id or "unknown"}',
                    f'aliases={", ".join(user_contact_aliases(user)) or "none"}',
                ]
            )
        )
        lines.append(f'  Profile:\n{profile_summary_text(user)}')
    lines.extend(['', '# Contact Registry', contact_registry_text(users, settings.zalo_group_entries)])
    return '\n'.join(lines)


def _tool_system_prompt(actor: User) -> str:
    return (
        f'{persona_text()}\n\n'
        'Bạn đang ở nhánh tool-calling của trợ lý Zalo cho Task Manager. '
        'Bạn có quyền dùng tools để tìm task, list task, tạo task, approve task, đổi status task, sửa field task, tạo/quản lý reminder, và gửi tin nhắn Zalo hộ người dùng. '
        'Luôn dùng tool khi người dùng muốn thao tác với task hoặc hỏi danh sách task. '
        'Chỉ dùng send_message khi người dùng yêu cầu nhắn/gửi/chuyển lời tới một người hoặc group khác một cách rõ ràng. '
        'Nếu người dùng nói "gửi link", "cho anh link", "gửi chi tiết task" trong chính cuộc chat hiện tại, hãy trả lời trong chat, không dùng send_message. '
        'Khi trả lời link hoặc chi tiết task, dùng format plain text từ tool result, không dùng markdown link và không tự bịa domain. '
        'Không tự bịa task_id. Nếu người dùng muốn approve/review mà chưa xác định rõ task, hãy dùng find_tasks trước. '
        'Nếu người dùng muốn đổi status mà chưa xác định rõ task, hãy dùng find_tasks trước rồi update_task_status. '
        'Nếu người dùng muốn sửa deadline/assignee/description/link/field khác của task cũ, hãy dùng find_tasks trước rồi update_task_fields. '
        'Nếu người dùng muốn nhắc việc, check-in hằng ngày, nudge task lặp lại, hoặc tắt/sửa reminder, hãy dùng reminder tools. '
        'Chỉ gọi create_task khi người dùng thực sự muốn tạo task mới. '
        'Nếu tool trả ok=false hoặc một phần tool fail, phải nói rõ phần chưa làm được; không được nói đã cập nhật field bị fail. '
        'Nếu câu mơ hồ, không chắc, hoặc có nhiều task match, đừng tự quyết định; hãy hỏi lại ngắn gọn. '
        f'Người dùng hiện tại: {actor.name} ({actor.username}), role={actor.role or "unknown"}. '
        f'Admin={"yes" if _is_admin(actor) else "no"}.'
    )


def _tool_user_prompt(
    *,
    actor: User,
    text: str,
    user_directory_text: str,
    recent_conversation: str,
    conversation_state: str,
    memory_text: str,
    current_channel: NotificationChannel | None = None,
    current_target_id: str | None = None,
) -> str:
    contact_parts = [f'Actor custom prompt:\n{contact_prompt_text_for_user(actor)}']
    if current_channel == NotificationChannel.group and current_target_id:
        group_name = dict(settings.zalo_group_entries).get(current_target_id, current_target_id)
        contact_parts.append(f'Current group custom prompt:\n{contact_prompt_text_for_group(current_target_id, group_name)}')
    contact_context = '\n\n'.join(contact_parts)
    return (
        f'Actor: {actor.name} | username={actor.username} | role={actor.role or "unknown"}\n'
        f'Profile markdown:\n{profile_text(actor)}\n\n'
        f'Contact custom context:\n{contact_context}\n\n'
        f'Active user directory:\n{user_directory_text}\n\n'
        f'Conversation state:\n{conversation_state}\n\n'
        f'Known memory facts:\n{memory_text}\n\n'
        f'Recent conversation in this thread:\n{recent_conversation}\n\n'
        f'Message: {text}\n'
        'Nếu đây chỉ là chat thông thường, trả lời trực tiếp không cần tool. '
        'Nếu đây là thao tác task, hãy dùng tool phù hợp trước rồi mới trả lời.'
    )


def _tool_task_payload(task: Task) -> dict[str, Any]:
    assignee_name = task.assignee.name if task.assignee else None
    if not assignee_name and task.assigned_to:
        session = object_session(task)
        if session:
            assignee = session.get(User, str(task.assigned_to))
            assignee_name = assignee.name if assignee else None
    return {
        'id': task.id,
        'title': task.title,
        'status': task.status.value,
        'assignee': assignee_name,
        'assigned_to': task.assigned_to,
        'due_date': task.due_date.isoformat() if task.due_date else None,
        'shop': task.shop.name if task.shop else None,
        'type': task.task_type.name if task.task_type else None,
    }


def _task_url(task_id: int) -> str | None:
    if settings.vikunja_enabled:
        template = (settings.vikunja_task_url_template or '').strip()
        if template:
            return template.format(project_id=settings.vikunja_project_id or '', task_id=task_id)
        base = (settings.vikunja_public_url or settings.vikunja_api_url or '').strip().rstrip('/')
        if base:
            return f'{base}/tasks/{task_id}'

    base = (settings.task_public_base_url or '').strip().rstrip('/')
    if not base:
        return None
    if '{task_id}' in base:
        return base.replace('{task_id}', str(task_id))
    return f'{base}/today?task={task_id}'


def _use_vikunja_tasks() -> bool:
    return settings.vikunja_enabled


def _task_due_datetime_value(due_date: date | None) -> str | None:
    if not due_date:
        return None
    return datetime.combine(due_date, time(hour=23, minute=59), tzinfo=ZoneInfo(settings.app_timezone)).isoformat()


def _date_from_vikunja(value: Any) -> date | None:
    if not value:
        return None
    raw = str(value)
    try:
        if 'T' in raw:
            return datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(ZoneInfo(settings.app_timezone)).date()
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _status_from_vikunja_payload(task: dict[str, Any], task_status_map: dict[int, str] | None = None) -> str:
    return vikunja_task_status_from_payload(task, task_status_map)


def _vikunja_assignee_social_ids(db: Session, task: dict[str, Any]) -> list[str]:
    assignees = task.get('assignees')
    if not isinstance(assignees, list):
        return []

    social_ids: list[str] = []
    for assignee in assignees:
        if not isinstance(assignee, dict):
            continue
        try:
            vikunja_user_id = int(assignee.get('id'))
        except (TypeError, ValueError):
            continue
        mapping = db.scalar(select(VikunjaUserMapping).where(VikunjaUserMapping.vikunja_user_id == vikunja_user_id))
        if mapping and mapping.social_user_id:
            social_ids.append(mapping.social_user_id)
    return social_ids


def _vikunja_assignee_names(db: Session, task: dict[str, Any]) -> list[str]:
    names: list[str] = []
    assignees = task.get('assignees')
    if not isinstance(assignees, list):
        return names

    for assignee in assignees:
        if not isinstance(assignee, dict):
            continue
        try:
            vikunja_user_id = int(assignee.get('id'))
        except (TypeError, ValueError):
            vikunja_user_id = None
        if vikunja_user_id is not None:
            mapping = db.scalar(select(VikunjaUserMapping).where(VikunjaUserMapping.vikunja_user_id == vikunja_user_id))
            if mapping:
                names.append(mapping.display_name or mapping.username)
                continue
        display = assignee.get('name') or assignee.get('username') or assignee.get('email')
        if display:
            names.append(str(display))
    return names


def _tool_vikunja_task_payload(
    db: Session,
    task: dict[str, Any],
    *,
    task_status_map: dict[int, str] | None = None,
) -> dict[str, Any]:
    task_id = int(task.get('id'))
    due_date = _date_from_vikunja(task.get('due_date'))
    assignee_names = _vikunja_assignee_names(db, task)
    assignee_social_ids = _vikunja_assignee_social_ids(db, task)
    try:
        priority_number = int(task.get('priority') or 0)
    except (TypeError, ValueError):
        priority_number = 0
    return {
        'id': task_id,
        'title': str(task.get('title') or f'Task #{task_id}'),
        'status': _status_from_vikunja_payload(task, task_status_map),
        'assignee': ', '.join(assignee_names) if assignee_names else None,
        'assigned_to': assignee_social_ids[0] if assignee_social_ids else None,
        'due_date': due_date.isoformat() if due_date else None,
        'shop': None,
        'type': None,
        'priority': VIKUNJA_PRIORITY_TO_TASK_PRIORITY.get(priority_number, TaskPriority.medium.value),
    }


def _vikunja_project_id(db: Session) -> int:
    client = get_vikunja_client()
    return ensure_vikunja_project(db, client)


def _resolve_vikunja_task_id(db: Session, task_id: int) -> int:
    mapping = db.scalar(select(VikunjaTaskMapping).where(VikunjaTaskMapping.local_task_id == task_id))
    if mapping and mapping.vikunja_task_id:
        return int(mapping.vikunja_task_id)
    return task_id


def _resolve_vikunja_assignee(db: Session, token: str | None) -> tuple[User | None, int | None, str | None]:
    user = _resolve_user(db, token)
    if not user:
        return None, None, f'Không tìm thấy assignee {token}.'
    mapping = db.scalar(select(VikunjaUserMapping).where(VikunjaUserMapping.social_user_id == user.id))
    if not mapping or not mapping.vikunja_user_id:
        return user, None, f'{user.name} chưa được sync user sang Task Manager mới.'
    return user, int(mapping.vikunja_user_id), None


def _actor_can_access_vikunja_task(db: Session, *, actor: User, task: dict[str, Any]) -> bool:
    if _is_admin(actor):
        return True
    return actor.id in _vikunja_assignee_social_ids(db, task)


def _actor_can_edit_vikunja_task(db: Session, *, actor: User, task: dict[str, Any]) -> bool:
    return _actor_can_access_vikunja_task(db, actor=actor, task=task)


def _vikunja_status_payload(status_token: str) -> tuple[dict[str, Any] | None, str | None]:
    if status_token not in {status.value for status in TaskStatus}:
        return None, f'Unknown status: {status_token}'

    updates: dict[str, Any] = {'done': status_token == TaskStatus.done.value}
    bucket_id = settings.vikunja_status_bucket_map.get(status_token)
    if bucket_id:
        updates['bucket_id'] = bucket_id
    elif status_token != TaskStatus.done.value:
        return None, f'Chưa cấu hình bucket cho status "{status_token}" trong Task Manager mới.'
    return updates, None


def _format_vikunja_changed_fields(changes: dict[str, Any]) -> list[str]:
    translated: list[str] = []
    for key in changes:
        if key == 'assignees':
            translated.append('assigned_to')
        elif key == 'bucket_id':
            translated.append('status')
        else:
            translated.append(key)
    return sorted(set(translated))


def _task_manager_error(exc: Exception) -> str:
    return str(exc).replace('Vikunja ', 'Task Manager ')


def _format_task_detail(task: dict[str, Any], *, compact: bool = False) -> str:
    task_id = task.get('id')
    title = task.get('title') or f'Task #{task_id}'
    lines = [f'Tên task: {title}']
    if not compact:
        assignee = task.get('assignee') or 'Unassigned'
        status_value = task.get('status') or 'unknown'
        due_date = task.get('due_date') or 'Chưa có'
        lines.extend(
            [
                f'Phụ trách: {assignee}',
                f'Trạng thái: {status_value}',
                f'Deadline: {due_date}',
            ]
        )
    url = _task_url(int(task['id'])) if task.get('id') is not None else None
    if url:
        lines.append(f'Link task: {url}')
    return '\n'.join(lines)


def _attach_task_format(result: dict[str, Any], *, compact: bool = False) -> dict[str, Any]:
    task = result.get('task')
    if isinstance(task, dict) and task.get('id') is not None:
        result['task_url'] = _task_url(int(task['id']))
        result['formatted_task'] = _format_task_detail(task, compact=compact)
    return result


def _tool_find_vikunja_tasks(db: Session, *, actor: User, query: str, status_token: str | None, limit: int) -> dict[str, Any]:
    client = get_vikunja_client()
    project_id = _vikunja_project_id(db)
    normalized_query = (query or '').strip()
    max_items = max(1, min(limit, 10))

    if normalized_query.startswith('#') and normalized_query[1:].isdigit():
        candidates = [client.get_task(int(normalized_query[1:]))]
    elif normalized_query.isdigit():
        candidates = [client.get_task(int(normalized_query))]
    else:
        candidates = client.list_all_project_tasks(project_id)

    task_status_map = build_vikunja_task_status_map(client, project_id)
    normalized = _normalize_lookup(normalized_query)
    matched_user_ids = {user.id for user in _matching_users_for_token(db, normalized_query)}
    tasks: list[dict[str, Any]] = []

    for task in candidates:
        if not isinstance(task, dict) or task.get('id') is None:
            continue
        if int(task.get('project_id') or project_id) != project_id:
            continue
        if not _actor_can_access_vikunja_task(db, actor=actor, task=task):
            continue

        payload = _tool_vikunja_task_payload(db, task, task_status_map=task_status_map)
        if status_token and payload['status'] != status_token:
            continue

        if normalized_query and not (normalized_query.startswith('#') and normalized_query[1:].isdigit()) and not normalized_query.isdigit():
            haystacks = [
                str(payload.get('title') or ''),
                str(task.get('description') or ''),
                str(payload.get('assignee') or ''),
                str(payload.get('assigned_to') or ''),
            ]
            assignee_ids = set(_vikunja_assignee_social_ids(db, task))
            if normalized and not any(normalized in _normalize_lookup(value) for value in haystacks) and not (matched_user_ids & assignee_ids):
                continue

        tasks.append(payload)
        if len(tasks) >= max_items:
            break

    response = {'ok': True, 'count': len(tasks), 'tasks': tasks}
    if len(tasks) == 1:
        task_payload = tasks[0]
        response['task'] = task_payload
        response['task_url'] = _task_url(int(task_payload['id']))
        response['formatted_task'] = _format_task_detail(task_payload, compact=True)
    return response


def _tool_list_vikunja_tasks(db: Session, *, actor: User, view: str) -> dict[str, Any]:
    client = get_vikunja_client()
    project_id = _vikunja_project_id(db)
    normalized_view = view if view in {'today', 'inbox', 'review', 'logbook'} else 'today'
    today = _today()
    tasks: list[dict[str, Any]] = []
    task_status_map = build_vikunja_task_status_map(client, project_id)

    for task in client.list_all_project_tasks(project_id):
        if not isinstance(task, dict) or task.get('id') is None:
            continue
        if not _actor_can_access_vikunja_task(db, actor=actor, task=task):
            continue
        payload = _tool_vikunja_task_payload(db, task, task_status_map=task_status_map)
        due_date = date.fromisoformat(payload['due_date']) if payload.get('due_date') else None
        is_done = payload['status'] == TaskStatus.done.value

        include = False
        if normalized_view == 'today':
            include = not is_done and due_date is not None and due_date <= today
        elif normalized_view == 'inbox':
            include = not is_done
        elif normalized_view == 'review':
            include = payload['status'] == TaskStatus.review.value
        elif normalized_view == 'logbook':
            include = is_done

        if include:
            tasks.append(payload)

    tasks.sort(key=lambda item: (item.get('due_date') or '9999-12-31', item.get('id') or 0))
    group_title = {
        'today': 'Today + overdue',
        'inbox': 'Open tasks',
        'review': 'Review',
        'logbook': 'Logbook',
    }[normalized_view]
    return {'ok': True, 'view': normalized_view, 'groups': [{'key': normalized_view, 'title': group_title, 'tasks': tasks[:30]}]}


def _tool_create_vikunja_task(
    db: Session,
    *,
    actor: User,
    title: str,
    assignee_token: str | None,
    shop_token: str | None = None,
    type_token: str | None = None,
    due_token: str | None = None,
    priority_token: str | None = None,
) -> dict[str, Any]:
    clean_title = title.strip()
    if not clean_title:
        return {'ok': False, 'error': 'Task title cannot be empty.'}

    shop = _resolve_shop(db, shop_token) if shop_token else None
    task_type = _resolve_task_type(db, type_token) if type_token else None
    if shop_token and not shop:
        return {'ok': False, 'error': f'Không tìm thấy shop {shop_token}.'}
    if type_token and not task_type:
        return {'ok': False, 'error': f'Không tìm thấy task type {type_token}.'}
    clean_title = _auto_prefix_title_for_type(clean_title, task_type)

    assignee_user: User | None = None
    assignee_id: int | None = None
    if assignee_token:
        if not _is_admin(actor):
            return {'ok': False, 'error': 'Members cannot assign tasks to other users.'}
        assignee_user, assignee_id, error = _resolve_vikunja_assignee(db, assignee_token)
        if error:
            return {'ok': False, 'error': error}
    elif not _is_admin(actor):
        assignee_user, assignee_id, error = _resolve_vikunja_assignee(db, actor.username)
        if error:
            return {'ok': False, 'error': error}

    due_date = _parse_date_token(due_token)
    priority_token = priority_token if priority_token in TASK_PRIORITY_TO_VIKUNJA_PRIORITY else TaskPriority.medium.value
    project_id = _vikunja_project_id(db)
    payload: dict[str, Any] = {
        'title': clean_title,
        'project_id': project_id,
        'done': False,
        'priority': TASK_PRIORITY_TO_VIKUNJA_PRIORITY[priority_token],
    }
    bucket_id = settings.vikunja_status_bucket_map.get(TaskStatus.todo.value) or settings.vikunja_bucket_inbox_id
    if bucket_id:
        payload['bucket_id'] = bucket_id
    if due_date:
        payload['due_date'] = _task_due_datetime_value(due_date)
    if assignee_id:
        payload['assignees'] = [{'id': assignee_id}]
    metadata = [item for item in (f'Shop: {shop.name}' if shop else None, f'Type: {task_type.name}' if task_type else None) if item]
    if metadata:
        payload['description'] = 'Task metadata:\n' + '\n'.join(f'- {item}' for item in metadata)

    task = get_vikunja_client().create_task(project_id, payload)
    task_status_map = build_vikunja_task_status_map(get_vikunja_client(), project_id)
    task_payload = _tool_vikunja_task_payload(db, task, task_status_map=task_status_map)
    if assignee_user and not task_payload.get('assignee'):
        task_payload['assignee'] = assignee_user.name
        task_payload['assigned_to'] = assignee_user.id
    return _attach_task_format({'ok': True, 'task': task_payload})


def _tool_update_vikunja_task_status(db: Session, *, actor: User, task_id: int, status_token: str) -> dict[str, Any]:
    client = get_vikunja_client()
    resolved_task_id = _resolve_vikunja_task_id(db, task_id)
    task = client.get_task(resolved_task_id)
    if not _actor_can_edit_vikunja_task(db, actor=actor, task=task):
        return {'ok': False, 'error': 'Members can only update their own tasks.'}

    task_status_map = build_vikunja_task_status_map(client, _vikunja_project_id(db))
    previous_status = _status_from_vikunja_payload(task, task_status_map)
    if not _is_admin(actor):
        next_status = TaskStatus(status_token) if status_token in {status.value for status in TaskStatus} else None
        if next_status is None:
            return {'ok': False, 'error': f'Unknown status: {status_token}'}
        fake_previous_status = TaskStatus(previous_status) if previous_status in {status.value for status in TaskStatus} else TaskStatus.todo
        fake_task = Task(title=str(task.get('title') or 'Task'), status=fake_previous_status, assigned_to=actor.id)
        error = _validate_tool_status_transition(fake_task, next_status, actor)
        if error:
            return {'ok': False, 'error': error}

    updates, error = _vikunja_status_payload(status_token)
    if error:
        return {'ok': False, 'error': error}
    updated = client.update_task(resolved_task_id, updates or {})
    task_payload = _tool_vikunja_task_payload(db, updated)
    task_payload['status'] = status_token
    return _attach_task_format(
        {
            'ok': True,
            'task': task_payload,
            'previous_status': previous_status,
            'changed_fields': ['status'],
        }
    )


def _tool_approve_vikunja_task(db: Session, *, actor: User, task_id: int) -> dict[str, Any]:
    if not _is_admin(actor):
        return {'ok': False, 'error': 'Only admins can approve review tasks.'}
    client = get_vikunja_client()
    resolved_task_id = _resolve_vikunja_task_id(db, task_id)
    task = client.get_task(resolved_task_id)
    task_status_map = build_vikunja_task_status_map(client, _vikunja_project_id(db))
    previous_status = _status_from_vikunja_payload(task, task_status_map)
    if previous_status != TaskStatus.review.value:
        return {'ok': False, 'error': 'Only tasks in review can be approved to ready.'}
    updates, error = _vikunja_status_payload(TaskStatus.ready.value)
    if error:
        return {'ok': False, 'error': error}
    updated = client.update_task(resolved_task_id, updates or {})
    task_payload = _tool_vikunja_task_payload(db, updated)
    task_payload['status'] = TaskStatus.ready.value
    return _attach_task_format({'ok': True, 'task': task_payload, 'previous_status': previous_status})


def _tool_update_vikunja_task_fields(
    db: Session,
    *,
    actor: User,
    task_id: int,
    arguments: dict[str, Any],
    original_text: str | None,
) -> dict[str, Any]:
    client = get_vikunja_client()
    resolved_task_id = _resolve_vikunja_task_id(db, task_id)
    task = client.get_task(resolved_task_id)
    if not _actor_can_edit_vikunja_task(db, actor=actor, task=task):
        return {'ok': False, 'error': 'Members can only edit their own tasks.'}

    updates: dict[str, Any] = {}
    task_status_map = build_vikunja_task_status_map(client, _vikunja_project_id(db))
    previous_status = _status_from_vikunja_payload(task, task_status_map)

    if 'title' in arguments:
        title = str(arguments.get('title') or '').strip()
        if not title:
            return {'ok': False, 'error': 'Task title cannot be empty.'}
        updates['title'] = title

    if 'description' in arguments:
        updates['description'] = str(arguments.get('description') or '').strip()

    metadata_updates: list[str] = []
    if arguments.get('clear_shop') or _is_clear_token(str(arguments.get('shop_token') or '')):
        metadata_updates.append('Shop: None')
    elif 'shop_token' in arguments and str(arguments.get('shop_token') or '').strip():
        shop = _resolve_shop(db, str(arguments.get('shop_token') or ''))
        if not shop:
            return {'ok': False, 'error': f'Không tìm thấy shop {arguments.get("shop_token")}.'}
        metadata_updates.append(f'Shop: {shop.name}')

    if arguments.get('clear_type') or _is_clear_token(str(arguments.get('type_token') or '')):
        updates['title'] = _replace_type_prefix_for_title(db, str(updates.get('title') or task.get('title') or ''), None)
        metadata_updates.append('Type: None')
    elif 'type_token' in arguments and str(arguments.get('type_token') or '').strip():
        task_type = _resolve_task_type(db, str(arguments.get('type_token') or ''))
        if not task_type:
            return {'ok': False, 'error': f'Không tìm thấy task type {arguments.get("type_token")}.'}
        updates['title'] = _replace_type_prefix_for_title(db, str(updates.get('title') or task.get('title') or ''), task_type)
        metadata_updates.append(f'Type: {task_type.name}')

    if arguments.get('clear_assignee'):
        if not _is_admin(actor):
            return {'ok': False, 'error': 'Members cannot reassign tasks.'}
        updates['assignees'] = []
    elif 'assignee_token' in arguments and str(arguments.get('assignee_token') or '').strip():
        if not _is_admin(actor):
            return {'ok': False, 'error': 'Members cannot reassign tasks.'}
        assignee, vikunja_user_id, error = _resolve_vikunja_assignee(db, str(arguments.get('assignee_token') or ''))
        if error:
            return {'ok': False, 'error': error}
        if assignee and original_text and not _user_is_mentioned_in_text(assignee, original_text):
            return {
                'ok': False,
                'error': (
                    f'Người nhận {assignee.name} không xuất hiện rõ trong yêu cầu gốc. '
                    'Không đổi assign để tránh gán nhầm người.'
                ),
            }
        updates['assignees'] = [{'id': vikunja_user_id}]

    if arguments.get('clear_due_date') or _is_clear_token(str(arguments.get('due_token') or '')):
        updates['due_date'] = None
    elif 'due_token' in arguments and str(arguments.get('due_token') or '').strip():
        due_date = _parse_date_token(str(arguments.get('due_token') or ''))
        if not due_date:
            return {'ok': False, 'error': f'Không hiểu due date "{arguments.get("due_token")}".'}
        updates['due_date'] = _task_due_datetime_value(due_date)

    if 'priority' in arguments and str(arguments.get('priority') or '').strip():
        priority_token = str(arguments.get('priority') or '').strip()
        if priority_token not in TASK_PRIORITY_TO_VIKUNJA_PRIORITY:
            return {'ok': False, 'error': f'Unknown priority: {priority_token}'}
        updates['priority'] = TASK_PRIORITY_TO_VIKUNJA_PRIORITY[priority_token]

    if 'status' in arguments and str(arguments.get('status') or '').strip():
        status_updates, error = _vikunja_status_payload(str(arguments.get('status') or '').strip())
        if error:
            return {'ok': False, 'error': error}
        updates.update(status_updates or {})

    if 'link_url' in arguments and str(arguments.get('link_url') or '').strip():
        clean_url = _validate_url(str(arguments.get('link_url') or ''))
        current_description = str(task.get('description') or '').strip()
        link_name = str(arguments.get('link_name') or '').strip() or urlparse(clean_url).netloc or 'Link'
        link_line = f'{link_name}: {clean_url}'
        updates['description'] = f'{current_description}\n\nLinks:\n- {link_line}'.strip()

    if metadata_updates:
        current_description = str(updates.get('description') if 'description' in updates else task.get('description') or '').strip()
        updates['description'] = f'{current_description}\n\nTask metadata update:\n' + '\n'.join(f'- {item}' for item in metadata_updates)
        updates['description'] = updates['description'].strip()

    if not updates:
        return {'ok': False, 'error': 'Không có field nào để cập nhật.'}

    updated = client.update_task(resolved_task_id, updates)
    task_payload = _tool_vikunja_task_payload(db, updated, task_status_map=task_status_map)
    if 'status' in arguments and str(arguments.get('status') or '').strip():
        task_payload['status'] = str(arguments.get('status') or '').strip()
    return _attach_task_format(
        {
            'ok': True,
            'task': task_payload,
            'previous_status': previous_status,
            'changed_fields': _format_vikunja_changed_fields(updates),
            'attachments_added': [],
        }
    )


def _tool_find_tasks(db: Session, *, actor: User, query: str, status_token: str | None, limit: int) -> dict[str, Any]:
    if _use_vikunja_tasks():
        try:
            return _tool_find_vikunja_tasks(db, actor=actor, query=query, status_token=status_token, limit=limit)
        except (VikunjaConfigError, RuntimeError, ValueError) as exc:
            return {'ok': False, 'error': _task_manager_error(exc)}

    stmt = (
        select(Task)
        .options(joinedload(Task.assignee), joinedload(Task.shop), joinedload(Task.task_type))
        .order_by(Task.updated_at.desc(), Task.id.desc())
    )
    if not _is_admin(actor):
        stmt = stmt.where(Task.assigned_to == actor.id)

    normalized_query = (query or '').strip()
    if normalized_query.startswith('#') and normalized_query[1:].isdigit():
        stmt = stmt.where(Task.id == int(normalized_query[1:]))
    elif normalized_query.isdigit():
        stmt = stmt.where(Task.id == int(normalized_query))
    else:
        like_query = f'%{normalized_query.lower()}%'
        matched_user_ids = [user.id for user in _matching_users_for_token(db, normalized_query)]
        search_filters = [func.lower(Task.title).like(like_query)]
        if matched_user_ids:
            search_filters.append(Task.assigned_to.in_(matched_user_ids))
        stmt = stmt.where(or_(*search_filters))

    if status_token and status_token in {status.value for status in TaskStatus}:
        stmt = stmt.where(Task.status == TaskStatus(status_token))

    tasks = db.scalars(stmt.limit(max(1, min(limit, 10)))).unique().all()
    response = {
        'ok': True,
        'count': len(tasks),
        'tasks': [_tool_task_payload(task) for task in tasks],
    }
    if len(response['tasks']) == 1:
        task_payload = response['tasks'][0]
        response['task'] = task_payload
        response['task_url'] = _task_url(int(task_payload['id']))
        response['formatted_task'] = _format_task_detail(task_payload, compact=True)
    return response


def _tool_list_tasks(db: Session, *, actor: User, view: str) -> dict[str, Any]:
    if _use_vikunja_tasks():
        try:
            return _tool_list_vikunja_tasks(db, actor=actor, view=view)
        except (VikunjaConfigError, RuntimeError, ValueError) as exc:
            return {'ok': False, 'error': _task_manager_error(exc)}

    normalized_view = view if view in {'today', 'inbox', 'review', 'logbook'} else 'today'
    result = list_tasks(
        db,
        normalized_view,
        actor_id=actor.id,
        actor_is_admin=_is_admin(actor),
    )
    groups = [
        {'key': group.key, 'title': group.title, 'tasks': [_tool_task_payload(task) for task in group.tasks]}
        for group in result.groups
    ]
    return {'ok': True, 'view': normalized_view, 'groups': groups}


def _tool_create_task(
    db: Session,
    *,
    actor: User,
    title: str,
    assignee_token: str | None,
    shop_token: str | None,
    type_token: str | None,
    due_token: str | None,
    priority_token: str | None,
) -> dict[str, Any]:
    if _use_vikunja_tasks():
        try:
            return _tool_create_vikunja_task(
                db,
                actor=actor,
                title=title,
                assignee_token=assignee_token,
                shop_token=shop_token,
                type_token=type_token,
                due_token=due_token,
                priority_token=priority_token,
            )
        except (VikunjaConfigError, RuntimeError, ValueError) as exc:
            return {'ok': False, 'error': _task_manager_error(exc)}

    priority_value = priority_token if priority_token in {'low', 'medium', 'high'} else 'medium'
    parsed = ParsedZaloCommand(
        action='add',
        title=title.strip(),
        assignee_token=(assignee_token or '').strip() or None,
        shop_token=(shop_token or '').strip() or None,
        type_token=(type_token or '').strip() or None,
        due_token=(due_token or '').strip() or None,
        priority=TaskPriority(priority_value),
    )
    task = _create_zalo_task(db, parsed, actor)
    db.commit()
    full_task = get_task_or_404(db, task.id) or task
    try:
        enqueue_task_created_notifications(db, full_task)
    except Exception:
        db.rollback()
    return _attach_task_format({'ok': True, 'task': _tool_task_payload(full_task)})


def _tool_approve_task(db: Session, *, actor: User, task_id: int) -> dict[str, Any]:
    if _use_vikunja_tasks():
        try:
            return _tool_approve_vikunja_task(db, actor=actor, task_id=task_id)
        except (VikunjaConfigError, RuntimeError, ValueError) as exc:
            return {'ok': False, 'error': _task_manager_error(exc)}

    task = get_task_or_404(db, task_id)
    if not task:
        return {'ok': False, 'error': 'Task not found.'}
    if not _is_admin(actor):
        return {'ok': False, 'error': 'Only admins can approve review tasks.'}
    if task.status != TaskStatus.review:
        return {'ok': False, 'error': 'Only tasks in review can be approved to ready.'}
    previous_status = task.status
    task.status = TaskStatus.ready
    db.add(task)
    db.commit()
    db.refresh(task)
    try:
        enqueue_task_status_transition_notifications(
            db,
            task=task,
            previous_status=previous_status,
            actor=actor,
        )
    except Exception:
        db.rollback()
    return _attach_task_format({'ok': True, 'task': _tool_task_payload(task), 'previous_status': previous_status.value})


def _validate_tool_status_transition(task: Task, next_status: TaskStatus, actor: User) -> str | None:
    if next_status == task.status:
        return None

    if _is_admin(actor):
        if next_status == TaskStatus.ready and task.status != TaskStatus.review:
            return 'Only tasks in review can be approved to ready.'
        return None

    if task.assigned_to != actor.id:
        return 'Members can only update their own tasks.'
    if next_status in MEMBER_ALLOWED_STATUSES:
        return None
    if task.status == TaskStatus.ready and next_status == TaskStatus.done:
        return None
    return 'Members can only move tasks up to review, or mark ready tasks as done.'


def _tool_update_task_status(db: Session, *, actor: User, task_id: int, status_token: str) -> dict[str, Any]:
    if _use_vikunja_tasks():
        try:
            return _tool_update_vikunja_task_status(db, actor=actor, task_id=task_id, status_token=status_token)
        except (VikunjaConfigError, RuntimeError, ValueError) as exc:
            return {'ok': False, 'error': _task_manager_error(exc)}

    task = get_task_or_404(db, task_id)
    if not task:
        return {'ok': False, 'error': 'Task not found.'}
    if status_token not in {status.value for status in TaskStatus}:
        return {'ok': False, 'error': f'Unknown status: {status_token}'}

    next_status = TaskStatus(status_token)
    error = _validate_tool_status_transition(task, next_status, actor)
    if error:
        return {'ok': False, 'error': error}

    previous_status = task.status
    task.status = next_status
    db.add(task)
    db.commit()
    db.refresh(task)
    if previous_status != task.status:
        try:
            enqueue_task_status_transition_notifications(
                db,
                task=task,
                previous_status=previous_status,
                actor=actor,
            )
        except Exception:
            db.rollback()
    return _attach_task_format({'ok': True, 'task': _tool_task_payload(task), 'previous_status': previous_status.value})


def _is_clear_token(value: str | None) -> bool:
    return _normalize_lookup(value or '') in {'none', 'null', 'clear', 'xoa', 'bo', 'remove', 'unassign'}


def _validate_url(value: str) -> str:
    clean = value.strip()
    parsed = urlparse(clean)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError('Link phải bắt đầu bằng http:// hoặc https://.')
    return clean


def _tool_update_task_fields(
    db: Session,
    *,
    actor: User,
    task_id: int,
    arguments: dict[str, Any],
    original_text: str | None,
) -> dict[str, Any]:
    if _use_vikunja_tasks():
        try:
            return _tool_update_vikunja_task_fields(
                db,
                actor=actor,
                task_id=task_id,
                arguments=arguments,
                original_text=original_text,
            )
        except (VikunjaConfigError, RuntimeError, ValueError) as exc:
            return {'ok': False, 'error': _task_manager_error(exc)}

    task = get_task_or_404(db, task_id)
    if not task:
        return {'ok': False, 'error': 'Task not found.'}

    if not _is_admin(actor) and task.assigned_to != actor.id:
        return {'ok': False, 'error': 'Members can only edit their own tasks.'}

    changed_fields: list[str] = []
    previous_status = task.status

    if 'title' in arguments:
        title = str(arguments.get('title') or '').strip()
        if not title:
            return {'ok': False, 'error': 'Task title cannot be empty.'}
        task.title = title
        changed_fields.append('title')

    if 'description' in arguments:
        task.description = str(arguments.get('description') or '').strip() or None
        changed_fields.append('description')

    if 'notes' in arguments:
        task.notes = str(arguments.get('notes') or '').strip() or None
        changed_fields.append('notes')

    if arguments.get('clear_assignee'):
        if not _is_admin(actor):
            return {'ok': False, 'error': 'Members cannot reassign tasks.'}
        task.assigned_to = None
        changed_fields.append('assigned_to')
    elif 'assignee_token' in arguments and str(arguments.get('assignee_token') or '').strip():
        if not _is_admin(actor):
            return {'ok': False, 'error': 'Members cannot reassign tasks.'}
        assignee = _resolve_user(db, str(arguments.get('assignee_token') or ''))
        if not assignee:
            return {'ok': False, 'error': f'Không tìm thấy assignee {arguments.get("assignee_token")}.'}
        if original_text and not _user_is_mentioned_in_text(assignee, original_text):
            return {
                'ok': False,
                'error': (
                    f'Người nhận {assignee.name} không xuất hiện rõ trong yêu cầu gốc. '
                    'Không đổi assign để tránh gán nhầm người.'
                ),
            }
        task.assigned_to = assignee.id
        changed_fields.append('assigned_to')

    if arguments.get('clear_shop') or _is_clear_token(str(arguments.get('shop_token') or '')):
        task.shop_id = None
        changed_fields.append('shop_id')
    elif 'shop_token' in arguments and str(arguments.get('shop_token') or '').strip():
        shop = _resolve_shop(db, str(arguments.get('shop_token') or ''))
        if not shop:
            return {'ok': False, 'error': f'Không tìm thấy shop {arguments.get("shop_token")}.'}
        task.shop_id = shop.id
        changed_fields.append('shop_id')

    if arguments.get('clear_type') or _is_clear_token(str(arguments.get('type_token') or '')):
        task.type_id = None
        changed_fields.append('type_id')
    elif 'type_token' in arguments and str(arguments.get('type_token') or '').strip():
        task_type = _resolve_task_type(db, str(arguments.get('type_token') or ''))
        if not task_type:
            return {'ok': False, 'error': f'Không tìm thấy task type {arguments.get("type_token")}.'}
        task.type_id = task_type.id
        changed_fields.append('type_id')

    if arguments.get('clear_due_date') or _is_clear_token(str(arguments.get('due_token') or '')):
        task.due_date = None
        changed_fields.append('due_date')
    elif 'due_token' in arguments and str(arguments.get('due_token') or '').strip():
        due_date = _parse_date_token(str(arguments.get('due_token') or ''))
        if not due_date:
            return {'ok': False, 'error': f'Không hiểu due date "{arguments.get("due_token")}".'}
        task.due_date = due_date
        changed_fields.append('due_date')

    if 'priority' in arguments and str(arguments.get('priority') or '').strip():
        priority_token = str(arguments.get('priority') or '').strip()
        if priority_token not in {'low', 'medium', 'high'}:
            return {'ok': False, 'error': f'Unknown priority: {priority_token}'}
        task.priority = TaskPriority(priority_token)
        changed_fields.append('priority')

    if 'status' in arguments and str(arguments.get('status') or '').strip():
        status_token = str(arguments.get('status') or '').strip()
        if status_token not in {status.value for status in TaskStatus}:
            return {'ok': False, 'error': f'Unknown status: {status_token}'}
        next_status = TaskStatus(status_token)
        error = _validate_tool_status_transition(task, next_status, actor)
        if error:
            return {'ok': False, 'error': error}
        task.status = next_status
        changed_fields.append('status')

    added_links: list[dict[str, Any]] = []
    if 'link_url' in arguments and str(arguments.get('link_url') or '').strip():
        clean_url = _validate_url(str(arguments.get('link_url') or ''))
        parsed = urlparse(clean_url)
        link_name = str(arguments.get('link_name') or '').strip() or parsed.netloc or 'Link'
        attachment = TaskAttachment(
            task_id=task.id,
            uploaded_by=actor.id,
            name=link_name,
            mime_type='text/uri-list',
            size_bytes=0,
            data_url=clean_url,
            storage_path=None,
            is_image=False,
        )
        db.add(attachment)
        added_links.append({'name': link_name, 'url': clean_url})
        changed_fields.append('attachment_links')

    db.add(task)
    db.commit()
    db.expire_all()
    full_task = get_task_or_404(db, task.id) or task

    if previous_status != full_task.status:
        try:
            enqueue_task_status_transition_notifications(
                db,
                task=full_task,
                previous_status=previous_status,
                actor=actor,
            )
        except Exception:
            db.rollback()

    non_status_changes = [field for field in changed_fields if field != 'status']
    if non_status_changes:
        try:
            enqueue_task_updated_notifications(db, task=full_task, actor=actor, changed_fields=non_status_changes)
        except Exception:
            db.rollback()

    return _attach_task_format({
        'ok': True,
        'task': _tool_task_payload(full_task),
        'previous_status': previous_status.value,
        'changed_fields': sorted(set(changed_fields)),
        'attachments_added': added_links,
    })


def _resolve_group_message_target(
    *,
    target_token: str | None,
    current_channel: NotificationChannel | None,
    current_target_id: str | None,
) -> str | None:
    token = (target_token or '').strip().casefold()
    if token in {'current', 'current_group', 'group hiện tại', 'nhóm hiện tại'}:
        if current_channel == NotificationChannel.group:
            return current_target_id
        return settings.zalo_group_id
    if token in {'default', 'default_group', 'main_group', 'group', 'nhóm'}:
        return settings.zalo_group_id or (current_target_id if current_channel == NotificationChannel.group else None)
    if target_token and target_token.strip():
        return target_token.strip()
    return settings.zalo_group_id or (current_target_id if current_channel == NotificationChannel.group else None)


def _tool_send_message(
    db: Session,
    *,
    actor: User,
    channel_token: str,
    target_token: str | None,
    message: str,
    current_channel: NotificationChannel | None,
    current_target_id: str | None,
    original_text: str | None,
) -> dict[str, Any]:
    normalized_channel = (channel_token or '').strip().casefold()
    clean_message = (message or '').strip()
    if not clean_message:
        return {'ok': False, 'error': 'Message is empty.'}

    if normalized_channel == NotificationChannel.user.value:
        target_user = _resolve_user(db, target_token)
        if not target_user:
            return {'ok': False, 'error': f'Không tìm thấy người nhận {target_token}.'}
        if original_text and not _user_is_mentioned_in_text(target_user, original_text):
            return {
                'ok': False,
                'error': (
                    f'Người nhận {target_user.name} không xuất hiện rõ trong yêu cầu gốc. '
                    'Không gửi để tránh nhắn nhầm người.'
                ),
            }
        if not target_user.zalo_user_id:
            return {'ok': False, 'error': f'{target_user.name} chưa có zalo_user_id.'}

        ok, status_code, body, error = send_zalo_text(
            channel=NotificationChannel.user,
            target_id=target_user.zalo_user_id,
            message=clean_message,
            context={
                'source': 'zalo_tool_agent',
                'command': 'send_message',
                'actor_id': actor.id,
                'target_user_id': target_user.id,
            },
        )
        return {
            'ok': ok,
            'channel': NotificationChannel.user.value,
            'target_id': target_user.zalo_user_id,
            'target_name': target_user.name,
            'status_code': status_code,
            'body': body,
            'error': error,
        }

    if normalized_channel == NotificationChannel.group.value:
        target_id = _resolve_group_message_target(
            target_token=target_token,
            current_channel=current_channel,
            current_target_id=current_target_id,
        )
        if not target_id:
            return {'ok': False, 'error': 'Không tìm thấy group để gửi tin.'}
        allowed_groups = settings.zalo_allowed_group_id_list
        if allowed_groups and target_id not in allowed_groups:
            return {'ok': False, 'error': 'Group này không nằm trong ZALO_ALLOWED_GROUP_IDS.'}

        ok, status_code, body, error = send_zalo_text(
            channel=NotificationChannel.group,
            target_id=target_id,
            message=clean_message,
            context={
                'source': 'zalo_tool_agent',
                'command': 'send_message',
                'actor_id': actor.id,
            },
        )
        return {
            'ok': ok,
            'channel': NotificationChannel.group.value,
            'target_id': target_id,
            'status_code': status_code,
            'body': body,
            'error': error,
        }

    return {'ok': False, 'error': f'Unknown send_message channel: {channel_token}'}


def _parse_schedule_time_token(value: str | None) -> time | None:
    if not value:
        return None
    clean = value.strip()
    match = re.fullmatch(r'(\d{1,2})(?::|h)?(\d{2})?', clean)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return time(hour=hour, minute=minute)


def _format_reminder_rule(rule: ReminderRule) -> str:
    if rule.schedule_type == ReminderScheduleType.interval:
        schedule = f'mỗi {rule.interval_minutes or 60} phút'
    else:
        schedule = f'hằng ngày {rule.schedule_time.strftime("%H:%M") if rule.schedule_time else "09:00"}'
    status_text = 'bật' if rule.enabled else 'tắt'
    return f'#{rule.id} {rule.name} — {rule.rule_type.value} — {schedule} — {status_text}'


def _ensure_reminder_tool_access(rule: ReminderRule, actor: User) -> str | None:
    if _is_admin(actor):
        return None
    if rule.user_id == actor.id:
        return None
    if rule.task and rule.task.assigned_to == actor.id:
        return None
    return 'Members chỉ được quản lý reminder của chính mình.'


def _tool_create_reminder_rule(
    db: Session,
    *,
    actor: User,
    arguments: dict[str, Any],
    current_channel: NotificationChannel | None,
    current_target_id: str | None,
) -> dict[str, Any]:
    rule_type_token = str(arguments.get('rule_type') or '').strip()
    if rule_type_token not in {item.value for item in ReminderRuleType}:
        return {'ok': False, 'error': f'Unknown reminder type: {rule_type_token}'}

    rule_type = ReminderRuleType(rule_type_token)
    task_id = arguments.get('task_id')
    interval_minutes = int(arguments.get('interval_minutes') or 60)
    if rule_type == ReminderRuleType.task_nudge:
        if not task_id:
            return {'ok': False, 'error': 'task_nudge cần task_id. Hãy tìm task trước.'}
        task = get_task_or_404(db, int(task_id))
        if not task:
            return {'ok': False, 'error': 'Task not found.'}
        if not _is_admin(actor) and task.assigned_to != actor.id:
            return {'ok': False, 'error': 'Members chỉ nhắc task của chính mình.'}
        rule = create_task_nudge_rule(
            db,
            actor=actor,
            task=task,
            interval_minutes=interval_minutes,
            name=str(arguments.get('name') or '').strip() or None,
        )
        return {'ok': True, 'rule': {'id': rule.id, 'summary': _format_reminder_rule(rule)}}

    target_channel: NotificationChannel | None = None
    target_id: str | None = None
    user_id: str | None = None
    target_token = str(arguments.get('target_token') or '').strip() or None
    target_channel_token = str(arguments.get('target_channel') or '').strip()

    if rule_type == ReminderRuleType.daily_group_digest or target_channel_token == NotificationChannel.group.value:
        if not _is_admin(actor):
            return {'ok': False, 'error': 'Only admins can create group reminders.'}
        target_channel = NotificationChannel.group
        target_id = _resolve_group_message_target(
            target_token=target_token,
            current_channel=current_channel,
            current_target_id=current_target_id,
        )
    elif target_channel_token == NotificationChannel.user.value and target_token:
        target_user = _resolve_user(db, target_token)
        if not target_user:
            return {'ok': False, 'error': f'Không tìm thấy user {target_token}.'}
        if not _is_admin(actor) and target_user.id != actor.id:
            return {'ok': False, 'error': 'Members chỉ tạo reminder cho chính mình.'}
        target_channel = NotificationChannel.user
        target_id = target_user.zalo_user_id
        user_id = target_user.id

    schedule_time = _parse_schedule_time_token(str(arguments.get('schedule_time') or '').strip() or None)
    values = {
        'name': str(arguments.get('name') or rule_type.value).strip(),
        'rule_type': rule_type.value,
        'target_channel': target_channel,
        'target_id': target_id,
        'user_id': user_id,
        'schedule_type': ReminderScheduleType.daily.value,
        'schedule_time': schedule_time,
        'max_runs_per_day': arguments.get('max_runs_per_day'),
    }
    rule = create_reminder_rule(db, actor=actor, values=values)
    return {'ok': True, 'rule': {'id': rule.id, 'summary': _format_reminder_rule(rule)}}


def _tool_list_reminder_rules(db: Session, *, actor: User, include_disabled: bool = False) -> dict[str, Any]:
    stmt = select(ReminderRule).options(joinedload(ReminderRule.task)).order_by(ReminderRule.created_at.desc(), ReminderRule.id.desc())
    if not include_disabled:
        stmt = stmt.where(ReminderRule.enabled.is_(True))
    if not _is_admin(actor):
        task_ids = select(Task.id).where(Task.assigned_to == actor.id)
        stmt = stmt.where(or_(ReminderRule.user_id == actor.id, ReminderRule.task_id.in_(task_ids)))
    rules = db.scalars(stmt.limit(20)).unique().all()
    return {'ok': True, 'count': len(rules), 'rules': [{'id': rule.id, 'summary': _format_reminder_rule(rule)} for rule in rules]}


def _tool_disable_reminder_rule(db: Session, *, actor: User, rule_id: int) -> dict[str, Any]:
    rule = db.get(ReminderRule, rule_id)
    if not rule:
        return {'ok': False, 'error': 'Reminder not found.'}
    if error := _ensure_reminder_tool_access(rule, actor):
        return {'ok': False, 'error': error}
    rule.enabled = False
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return {'ok': True, 'rule': {'id': rule.id, 'summary': _format_reminder_rule(rule)}}


def _tool_update_reminder_rule(db: Session, *, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    rule = db.get(ReminderRule, int(arguments.get('rule_id')))
    if not rule:
        return {'ok': False, 'error': 'Reminder not found.'}
    if error := _ensure_reminder_tool_access(rule, actor):
        return {'ok': False, 'error': error}

    values: dict[str, Any] = {}
    for key in ('name', 'enabled', 'interval_minutes', 'max_runs_per_day'):
        if key in arguments:
            values[key] = arguments[key]
    if 'schedule_time' in arguments:
        parsed_time = _parse_schedule_time_token(str(arguments.get('schedule_time') or ''))
        if not parsed_time:
            return {'ok': False, 'error': f'Không hiểu giờ nhắc {arguments.get("schedule_time")}.'}
        values['schedule_time'] = parsed_time
    rule = update_reminder_rule(db, rule=rule, values=values)
    return {'ok': True, 'rule': {'id': rule.id, 'summary': _format_reminder_rule(rule)}}


def _execute_tool_call(
    db: Session,
    *,
    actor: User,
    name: str,
    arguments: dict[str, Any],
    current_channel: NotificationChannel | None = None,
    current_target_id: str | None = None,
    original_text: str | None = None,
    task_context_text: str | None = None,
    state_data: dict | None = None,
) -> dict[str, Any]:
    try:
        if name == 'find_tasks':
            return _tool_find_tasks(
                db,
                actor=actor,
                query=str(arguments.get('query') or ''),
                status_token=str(arguments.get('status') or '').strip() or None,
                limit=int(arguments.get('limit') or 5),
            )
        if name == 'list_tasks':
            return _tool_list_tasks(db, actor=actor, view=str(arguments.get('view') or 'today'))
        if name == 'create_task':
            return _tool_create_task(
                db,
                actor=actor,
                title=str(arguments.get('title') or ''),
                assignee_token=str(arguments.get('assignee_token') or '').strip() or None,
                shop_token=str(arguments.get('shop_token') or '').strip() or None,
                type_token=str(arguments.get('type_token') or '').strip() or None,
                due_token=str(arguments.get('due_token') or '').strip() or None,
                priority_token=str(arguments.get('priority') or '').strip() or None,
            )
        if name == 'approve_task':
            return _tool_approve_task(db, actor=actor, task_id=int(arguments.get('task_id')))
        if name == 'update_task_status':
            return _tool_update_task_status(
                db,
                actor=actor,
                task_id=int(arguments.get('task_id')),
                status_token=str(arguments.get('status') or '').strip(),
            )
        if name == 'update_task_fields':
            grounded_arguments = _ground_update_arguments(
                db,
                arguments=arguments,
                current_text=original_text,
                state_data=state_data,
            )
            return _tool_update_task_fields(
                db,
                actor=actor,
                task_id=int(arguments.get('task_id')),
                arguments=grounded_arguments,
                original_text=task_context_text or original_text,
            )
        if name == 'send_message':
            return _tool_send_message(
                db,
                actor=actor,
                channel_token=str(arguments.get('channel') or '').strip(),
                target_token=str(arguments.get('target_token') or '').strip() or None,
                message=str(arguments.get('message') or ''),
                current_channel=current_channel,
                current_target_id=current_target_id,
                original_text=original_text,
            )
        if name == 'create_reminder_rule':
            return _tool_create_reminder_rule(
                db,
                actor=actor,
                arguments=arguments,
                current_channel=current_channel,
                current_target_id=current_target_id,
            )
        if name == 'list_reminder_rules':
            return _tool_list_reminder_rules(
                db,
                actor=actor,
                include_disabled=bool(arguments.get('include_disabled')),
            )
        if name == 'disable_reminder_rule':
            return _tool_disable_reminder_rule(db, actor=actor, rule_id=int(arguments.get('rule_id')))
        if name == 'update_reminder_rule':
            return _tool_update_reminder_rule(db, actor=actor, arguments=arguments)
    except (ValueError, TypeError) as exc:
        return {'ok': False, 'error': _task_manager_error(exc)}
    except PermissionError as exc:
        return {'ok': False, 'error': _task_manager_error(exc)}
    except HTTPException as exc:
        return {'ok': False, 'error': str(exc.detail)}
    return {'ok': False, 'error': f'Unknown tool: {name}'}


def _partial_tool_failure_message(tool_results: list[dict[str, Any]]) -> str | None:
    failed_results = [item for item in tool_results if not item.get('result', {}).get('ok')]
    if not failed_results:
        return None

    errors = [
        str(item.get('result', {}).get('error') or f'{item.get("name", "tool")} failed')
        for item in failed_results
    ]
    successful_names = [
        str(item.get('name'))
        for item in tool_results
        if item.get('result', {}).get('ok') and item.get('name')
    ]
    unique_successes = sorted(set(successful_names))
    if unique_successes:
        return (
            f'Em chưa hoàn tất hết nha: {"; ".join(errors)} '
            f'Phần đã chạy được: {", ".join(unique_successes)}.'
        )
    return f'Em chưa thực hiện được nha: {"; ".join(errors)}'


def _verified_update_success_message(tool_results: list[dict[str, Any]]) -> str | None:
    if any(not item.get('result', {}).get('ok') for item in tool_results):
        return None

    task: dict[str, Any] | None = None
    changed_fields: set[str] = set()
    for item in tool_results:
        name = str(item.get('name') or '')
        result = item.get('result') or {}
        candidate = result.get('task')
        if isinstance(candidate, dict) and name in {'update_task_fields', 'update_task_status', 'approve_task'}:
            task = candidate
        if name == 'update_task_status':
            changed_fields.add('status')
        for field in result.get('changed_fields') or []:
            changed_fields.add(str(field))

    if not task or not changed_fields:
        return None

    parts: list[str] = []
    if 'status' in changed_fields and task.get('status'):
        parts.append(f'status sang {task["status"]}')
    if 'assigned_to' in changed_fields:
        parts.append(f'người phụ trách sang {task.get("assignee") or "chưa gán"}')
    if 'due_date' in changed_fields:
        parts.append(f'deadline {task.get("due_date") or "trống"}')
    if 'description' in changed_fields:
        parts.append('description')
    if 'notes' in changed_fields:
        parts.append('notes')
    if 'shop_id' in changed_fields:
        parts.append(f'shop {task.get("shop") or "trống"}')
    if 'type_id' in changed_fields:
        parts.append(f'type {task.get("type") or "trống"}')
    if 'priority' in changed_fields:
        parts.append('priority')
    if 'attachment_links' in changed_fields:
        parts.append('link attachment')

    if not parts:
        return None
    detail = _format_task_detail(task)
    return f'Em đã cập nhật: {", ".join(parts)}.\n{detail}'


def _task_link_message_from_results(tool_results: list[dict[str, Any]], final_text: str) -> str | None:
    normalized = _normalize_lookup(final_text)
    wants_task_link = any(token in normalized for token in ('linktask', 'linkchitiettask', 'linkchitiet'))
    has_markdown_link = bool(re.search(r'\[[^\]]+\]\(https?://[^)]+\)', final_text))
    has_url = bool(re.search(r'https?://\S+', final_text))
    if not wants_task_link and not has_markdown_link and not has_url:
        return None

    for item in reversed(tool_results):
        result = item.get('result') or {}
        formatted = result.get('formatted_task')
        if formatted:
            return str(formatted)
        task = result.get('task')
        if isinstance(task, dict) and task.get('id') is not None:
            return _format_task_detail(task, compact=True)
        tasks = result.get('tasks') or []
        if len(tasks) == 1:
            return _format_task_detail(tasks[0], compact=True)
    return None


def _run_tool_agent(
    db: Session,
    *,
    actor: User,
    text: str,
    conversation_id: str | None = None,
    current_channel: NotificationChannel | None = None,
    current_target_id: str | None = None,
) -> dict[str, Any] | None:
    if not is_bot_llm_configured():
        return None

    user_directory_text = _active_user_directory_text(db)
    recent_conversation = recent_conversation_text(
        db,
        user_id=actor.id,
        conversation_id=conversation_id,
    )
    state_record = get_conversation_state(db, user_id=actor.id, conversation_id=conversation_id)
    state_data = dict(state_record.state_json or {}) if state_record else {}
    state_text = conversation_state_text(state_record)
    memory_text = recent_memory_text(db, user_id=actor.id)
    messages: list[dict[str, Any]] = [
        {'role': 'system', 'content': _tool_system_prompt(actor)},
        {
            'role': 'user',
            'content': _tool_user_prompt(
                actor=actor,
                text=text,
                user_directory_text=user_directory_text,
                recent_conversation=recent_conversation,
                conversation_state=state_text,
                memory_text=memory_text,
                current_channel=current_channel,
                current_target_id=current_target_id,
            ),
        },
    ]
    last_tool_result: dict[str, Any] | None = None
    last_tool_name: str | None = None
    tool_results: list[dict[str, Any]] = []
    task_context_text = '\n'.join(
        part for part in (recent_conversation, f'user: {text}') if part and part.strip()
    )

    for _ in range(4):
        try:
            response = complete_bot_conversation(
                messages=messages,
                tools=_tool_specs(),
                temperature=0.2,
            )
        except BotLLMError:
            return None

        messages.append(response.assistant_message)
        if not response.tool_calls:
            final_text = response.content.strip()
            if not final_text and last_tool_result:
                if last_tool_result.get('ok') and 'task' in last_tool_result:
                    task = last_tool_result['task']
                    return {
                        'handled': True,
                        'action': 'add' if last_tool_result.get('previous_status') is None else 'approve',
                        'message': f'Xong rồi. #{task["id"]} {task["title"]} hiện đang ở trạng thái {task["status"]}.',
                    }
                return None
            action = 'chat'
            if last_tool_name == 'create_task':
                action = 'add'
            elif last_tool_name == 'approve_task':
                action = 'approve'
            elif last_tool_name == 'update_task_status':
                action = 'status'
            elif last_tool_name == 'update_task_fields':
                action = 'edit'
            elif last_tool_name == 'list_tasks':
                action = 'list'
            elif last_tool_name == 'send_message':
                action = 'send_message'
                if last_tool_result and not last_tool_result.get('ok'):
                    error = str(last_tool_result.get('error') or 'Không rõ lỗi gửi tin.')
                    final_text = f'Em chưa gửi được tin nhắn nha: {error}'
            elif last_tool_name in {'create_reminder_rule', 'list_reminder_rules', 'disable_reminder_rule', 'update_reminder_rule'}:
                action = 'reminder'
            if action != 'send_message' and (failure_message := _partial_tool_failure_message(tool_results)):
                final_text = failure_message
            elif verified_message := _verified_update_success_message(tool_results):
                final_text = verified_message
            elif task_link_message := _task_link_message_from_results(tool_results, final_text):
                final_text = task_link_message
            if tool_results:
                next_state = _state_data_from_tool_results(existing_state=state_data, tool_results=tool_results)
                upsert_conversation_state(
                    db,
                    user_id=actor.id,
                    conversation_id=conversation_id,
                    state_data=next_state,
                )
            return {'handled': True, 'action': action, 'message': final_text}

        for tool_call in response.tool_calls:
            last_tool_name = tool_call.name
            result = _execute_tool_call(
                db,
                actor=actor,
                name=tool_call.name,
                arguments=tool_call.arguments,
                current_channel=current_channel,
                current_target_id=current_target_id,
                original_text=text,
                task_context_text=task_context_text,
                state_data=state_data,
            )
            last_tool_result = result
            tool_results.append({'name': tool_call.name, 'result': result})
            messages.append(
                {
                    'role': 'tool',
                    'tool_call_id': tool_call.id,
                    'content': json.dumps(result, ensure_ascii=False),
                }
            )
    return None


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


def _format_task_created(task: Task) -> str:
    return f'Đã tạo task:\n{_format_task_detail(_tool_task_payload(task))}'


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


def _maybe_handle_natural_add_fallback(
    *,
    db: Session,
    text: str,
    actor: User,
    channel: NotificationChannel | None,
    target_id: str | None,
    record: ZaloIncomingCommand,
) -> dict[str, Any] | None:
    natural_add = _parse_natural_add_command(text)
    if natural_add is None:
        return None
    return _handle_add_and_reply(
        db=db,
        parsed=natural_add,
        actor=actor,
        channel=channel,
        target_id=target_id,
        record=record,
    )


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
    body = _message_body_for_bot(payload)
    if body is None:
        return {'ok': True, 'ignored': True, 'reason': 'missing_bot_alias'}

    parsed = _parse_command_body(body)
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
        conversation_id = payload.conversation_id or payload.from_uid
        reminder_interaction = handle_reminder_interaction(
            db,
            actor=actor,
            text=body,
            conversation_id=conversation_id,
            message_id=payload.message_id,
            target_id=target_id,
        )
        if reminder_interaction and reminder_interaction.handled:
            message = reminder_interaction.message
            reply = _reply_to_conversation(
                channel=channel,
                target_id=target_id,
                message=message,
                context={
                    'source': 'reminder_interaction',
                    'interaction_type': reminder_interaction.interaction_type.value,
                    'run_id': reminder_interaction.run_id,
                    'message_id': payload.message_id,
                },
            )
            record.command = f'reminder_{reminder_interaction.interaction_type.value}'
            record.response_payload = {'message': message, 'reply': reply}
            db.commit()
            return {'ok': True, 'action': record.command, 'reply': reply}

        tool_text = body
        tool_outcome = _run_tool_agent(
            db,
            actor=actor,
            text=tool_text,
            conversation_id=conversation_id,
            current_channel=channel,
            current_target_id=target_id,
        )
        if tool_outcome and tool_outcome.get('handled'):
            action = str(tool_outcome.get('action') or 'chat')
            message = str(tool_outcome.get('message') or '').strip()
            reply = _reply_to_conversation(
                channel=channel,
                target_id=target_id,
                message=message,
                context={'source': 'zalo_tool_agent', 'command': action, 'message_id': payload.message_id},
            )
            store_conversation_message(
                db,
                user_id=actor.id,
                conversation_id=conversation_id,
                message_id=payload.message_id,
                role='user',
                content=tool_text,
                metadata={'source': 'zalo_tool_agent', 'command': action},
            )
            store_conversation_message(
                db,
                user_id=actor.id,
                conversation_id=conversation_id,
                message_id=payload.message_id,
                role='assistant',
                content=message,
                metadata={'source': 'zalo_tool_agent', 'command': action},
            )
            record.command = action
            record.response_payload = {'message': message, 'reply': reply}
            db.commit()
            return {'ok': True, 'action': action, 'reply': reply}

        if parsed.action == 'chat':
            fallback_add = _maybe_handle_natural_add_fallback(
                db=db,
                text=parsed.title or payload.text,
                actor=actor,
                channel=channel,
                target_id=target_id,
                record=record,
            )
            if fallback_add is not None:
                return fallback_add

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
        message = _task_manager_error(exc)
    except ValueError as exc:
        db.rollback()
        message = _task_manager_error(exc)

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
