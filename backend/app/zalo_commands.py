from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import hmac
import json
import re
import shlex
from typing import Any, Callable, Literal, TypeVar
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .bot_files import persona_text, profile_summary_text, profile_text
from .bot_llm import BotLLMError, complete_bot_conversation, is_bot_llm_configured
from .bot_memory import recent_conversation_text, recent_memory_text, store_conversation_message
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
from .notifications import enqueue_task_created_notifications, enqueue_task_status_transition_notifications, send_zalo_text
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
    return _resolve_unique_by_token(users, clean, lambda user: [user.id, user.username, user.name, user.zalo_user_id])


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
    ]


def _active_user_directory_text(db: Session) -> str:
    users = db.scalars(
        select(User).where(User.is_active.is_(True)).order_by(User.full_name.asc(), User.username.asc())
    ).all()
    if not users:
        return 'Không có user active nào.'

    lines: list[str] = []
    for user in users:
        lines.append(
            '- '
            + ' | '.join(
                [
                    f'name={user.name}',
                    f'username={user.username}',
                    f'role={user.role or "unknown"}',
                    f'zalo={user.zalo_user_id or "unknown"}',
                ]
            )
        )
        lines.append(f'  Profile:\n{profile_summary_text(user)}')
    return '\n'.join(lines)


def _tool_system_prompt(actor: User) -> str:
    return (
        f'{persona_text()}\n\n'
        'Bạn đang ở nhánh tool-calling của trợ lý Zalo cho Task Manager. '
        'Bạn có quyền dùng tools để tìm task, list task, tạo task, và approve task. '
        'Luôn dùng tool khi người dùng muốn thao tác với task hoặc hỏi danh sách task. '
        'Không tự bịa task_id. Nếu người dùng muốn approve/review mà chưa xác định rõ task, hãy dùng find_tasks trước. '
        'Chỉ gọi create_task khi người dùng thực sự muốn tạo task mới. '
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
    memory_text: str,
) -> str:
    return (
        f'Actor: {actor.name} | username={actor.username} | role={actor.role or "unknown"}\n'
        f'Profile markdown:\n{profile_text(actor)}\n\n'
        f'Active user directory:\n{user_directory_text}\n\n'
        f'Known memory facts:\n{memory_text}\n\n'
        f'Recent conversation in this thread:\n{recent_conversation}\n\n'
        f'Message: {text}\n'
        'Nếu đây chỉ là chat thông thường, trả lời trực tiếp không cần tool. '
        'Nếu đây là thao tác task, hãy dùng tool phù hợp trước rồi mới trả lời.'
    )


def _tool_task_payload(task: Task) -> dict[str, Any]:
    return {
        'id': task.id,
        'title': task.title,
        'status': task.status.value,
        'assignee': task.assignee.name if task.assignee else None,
        'assigned_to': task.assigned_to,
        'due_date': task.due_date.isoformat() if task.due_date else None,
        'shop': task.shop.name if task.shop else None,
        'type': task.task_type.name if task.task_type else None,
    }


def _tool_find_tasks(db: Session, *, actor: User, query: str, status_token: str | None, limit: int) -> dict[str, Any]:
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
        stmt = stmt.where(func.lower(Task.title).like(like_query))

    if status_token and status_token in {status.value for status in TaskStatus}:
        stmt = stmt.where(Task.status == TaskStatus(status_token))

    tasks = db.scalars(stmt.limit(max(1, min(limit, 10)))).unique().all()
    return {
        'ok': True,
        'count': len(tasks),
        'tasks': [_tool_task_payload(task) for task in tasks],
    }


def _tool_list_tasks(db: Session, *, actor: User, view: str) -> dict[str, Any]:
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
    return {'ok': True, 'task': _tool_task_payload(full_task)}


def _tool_approve_task(db: Session, *, actor: User, task_id: int) -> dict[str, Any]:
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
    return {'ok': True, 'task': _tool_task_payload(task), 'previous_status': previous_status.value}


def _execute_tool_call(db: Session, *, actor: User, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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
    except (ValueError, TypeError) as exc:
        return {'ok': False, 'error': str(exc)}
    except PermissionError as exc:
        return {'ok': False, 'error': str(exc)}
    except HTTPException as exc:
        return {'ok': False, 'error': str(exc.detail)}
    return {'ok': False, 'error': f'Unknown tool: {name}'}


def _run_tool_agent(
    db: Session,
    *,
    actor: User,
    text: str,
    conversation_id: str | None = None,
) -> dict[str, Any] | None:
    if not is_bot_llm_configured():
        return None

    user_directory_text = _active_user_directory_text(db)
    recent_conversation = recent_conversation_text(
        db,
        user_id=actor.id,
        conversation_id=conversation_id,
    )
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
                memory_text=memory_text,
            ),
        },
    ]
    last_tool_result: dict[str, Any] | None = None
    last_tool_name: str | None = None

    for _ in range(4):
        try:
            response = complete_bot_conversation(messages=messages, tools=_tool_specs(), temperature=0.2)
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
            elif last_tool_name == 'list_tasks':
                action = 'list'
            return {'handled': True, 'action': action, 'message': final_text}

        for tool_call in response.tool_calls:
            last_tool_name = tool_call.name
            result = _execute_tool_call(db, actor=actor, name=tool_call.name, arguments=tool_call.arguments)
            last_tool_result = result
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
        tool_text = _strip_bot_alias(payload.text, allow_plain_text=_is_direct_conversation(payload)) or payload.text
        conversation_id = payload.conversation_id or payload.from_uid
        tool_outcome = _run_tool_agent(db, actor=actor, text=tool_text, conversation_id=conversation_id)
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
