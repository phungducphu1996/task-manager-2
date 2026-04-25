from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from .bot_files import (
    append_event,
    contact_prompt_text_for_group,
    contact_prompt_text_for_user,
    contact_registry_text,
    ensure_bot_files,
    persona_text,
    profile_summary_text,
    profile_text,
    recent_events_text,
    update_profile_with_facts,
)
from .bot_llm import BotLLMError, generate_bot_reply, is_bot_llm_configured
from .bot_memory import (
    extract_important_event,
    extract_memory_facts,
    recent_conversation_text,
    recent_memory_text,
    store_conversation_message,
    upsert_memory_facts,
)
from .config import get_settings
from .models import NotificationChannel, User
from .notifications import send_zalo_text
from .services import list_tasks
from sqlalchemy import select

settings = get_settings()


@dataclass(slots=True)
class BotChatResult:
    message: str
    reply: dict[str, Any]
    used_llm: bool


def _is_admin(user: User) -> bool:
    return (user.role or '').strip().casefold() == 'admin'


def _task_groups_text(db: Session, *, actor: User, view: str) -> str:
    result = list_tasks(db, view, actor_id=actor.id, actor_is_admin=_is_admin(actor))
    lines: list[str] = []
    count = 0
    for group in result.groups:
        if count >= settings.bot_task_context_limit:
            break
        task_lines: list[str] = []
        for task in group.tasks:
            if count >= settings.bot_task_context_limit:
                break
            due = task.due_date.strftime('%d/%m/%Y') if task.due_date else 'không có due'
            assignee = task.assignee.name if task.assignee else 'Unassigned'
            task_type = task.task_type.name if task.task_type else 'No type'
            shop = task.shop.name if task.shop else 'No shop'
            task_lines.append(
                f'  - #{task.id} {task.title} | status={task.status.value} | assignee={assignee} | due={due} | type={task_type} | shop={shop}'
            )
            count += 1
        if task_lines:
            lines.append(f'{group.title}:')
            lines.extend(task_lines)
    if not lines:
        return f'{view}: không có task.'
    return '\n'.join(lines)


def _task_context_text(db: Session, *, actor: User) -> str:
    parts = [
        _task_groups_text(db, actor=actor, view='today'),
        _task_groups_text(db, actor=actor, view='inbox'),
    ]
    if _is_admin(actor):
        parts.append(_task_groups_text(db, actor=actor, view='review'))
    return '\n\n'.join(parts)


def _user_directory_text(db: Session) -> str:
    users = db.scalars(
        select(User).where(User.is_active.is_(True)).order_by(User.full_name.asc(), User.username.asc())
    ).all()
    if not users:
        return 'Không có user active nào.'

    blocks: list[str] = []
    for user in users:
        aliases = [user.name, user.username]
        if user.zalo_user_id:
            aliases.append(user.zalo_user_id)
        identity = ' | '.join(
            [
                f'name={user.name}',
                f'username={user.username}',
                f'role={user.role or "unknown"}',
                f'zalo={user.zalo_user_id or "unknown"}',
            ]
        )
        blocks.append(f'- {identity}\n  Profile:\n{profile_summary_text(user)}')
    return '\n'.join(blocks)


def _contact_context_text(
    db: Session,
    *,
    actor: User,
    reply_channel: NotificationChannel | None,
    reply_target_id: str | None,
) -> str:
    users = db.scalars(
        select(User).where(User.is_active.is_(True)).order_by(User.full_name.asc(), User.username.asc())
    ).all()
    parts = [
        contact_registry_text(users, settings.zalo_group_entries),
        f'Actor personal custom prompt:\n{contact_prompt_text_for_user(actor)}',
    ]
    if reply_channel == NotificationChannel.group and reply_target_id:
        group_name = dict(settings.zalo_group_entries).get(reply_target_id, reply_target_id)
        parts.append(f'Current group custom prompt:\n{contact_prompt_text_for_group(reply_target_id, group_name)}')
    return '\n\n'.join(parts)


def _fallback_reply(actor: User, incoming_text: str, *, task_context: str, memory_text: str) -> str:
    greeting = f'{actor.name} ơi, em nhận ra anh/chị đang hỏi em rồi nè.'
    task_block = task_context.split('\n\n', 1)[0].strip()
    memory_block = '' if memory_text.startswith('Chưa có') else f'\nEm đang nhớ về anh/chị:\n{memory_text}'
    return (
        f'{greeting}\n\n'
        f'Hiện em đang chạy ở chế độ cơ bản nên em chưa “trò chuyện thông minh” hết mức được.\n'
        f'Nhưng em vẫn thấy context task hiện tại như này:\n{task_block}{memory_block}\n\n'
        f'Tin nhắn vừa rồi của anh/chị là: "{incoming_text.strip()}".'
    )


def _system_prompt(persona: str) -> str:
    return (
        f'{persona}\n\n'
        'Bạn đang trả lời trong môi trường chat Zalo nội bộ văn phòng, có thể là group hoặc chat riêng. '
        'Hãy trả lời như một trợ lý hiểu người hỏi, hiểu task hiện tại, và nhớ bối cảnh gần đây. '
        'Ưu tiên tiếng Việt tự nhiên. Nếu câu hỏi có liên quan task, chỉ dùng dữ liệu được cung cấp. '
        'Nếu dữ liệu chưa đủ chắc, hãy nói rõ điều chưa chắc thay vì bịa.'
    )


def _user_prompt(
    *,
    actor: User,
    incoming_text: str,
    profile_markdown: str,
    memory_text: str,
    recent_conversation: str,
    events_text: str,
    task_context: str,
    user_directory_text: str,
    contact_context_text: str,
) -> str:
    return (
        f'Người đang hỏi:\n'
        f'- Name: {actor.name}\n'
        f'- Username: {actor.username}\n'
        f'- Role: {actor.role or "unknown"}\n'
        f'- Is admin: {"yes" if _is_admin(actor) else "no"}\n\n'
        f'Profile markdown:\n{profile_markdown}\n\n'
        f'Known memory facts:\n{memory_text}\n\n'
        f'Active user directory:\n{user_directory_text}\n\n'
        f'Contact registry and custom prompts:\n{contact_context_text}\n\n'
        f'Recent conversations:\n{recent_conversation}\n\n'
        f'Recent office events:\n{events_text}\n\n'
        f'Current task context:\n{task_context}\n\n'
        f'Tin nhắn mới nhất từ người dùng:\n{incoming_text}\n\n'
        'Yêu cầu trả lời:\n'
        '- Trả lời tự nhiên, ngắn gọn, hữu ích.\n'
        '- Nếu người dùng hỏi về task, ưu tiên trả lời theo task context.\n'
        '- Nếu phù hợp, nhắc đến tên người đó.\n'
        '- Không cần nói lộ raw markdown hay raw context.\n'
    )


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


def handle_zalo_chat(
    *,
    db: Session,
    actor: User,
    incoming_text: str,
    conversation_id: str | None,
    message_id: str | None,
    reply_channel: NotificationChannel | None,
    reply_target_id: str | None,
) -> BotChatResult:
    ensure_bot_files()

    store_conversation_message(
        db,
        user_id=actor.id,
        conversation_id=conversation_id,
        message_id=message_id,
        role='user',
        content=incoming_text,
        metadata={'source': 'zalo'},
    )

    extracted_facts = extract_memory_facts(incoming_text)
    stored_facts = upsert_memory_facts(db, user=actor, source_message_id=message_id, facts=extracted_facts)
    if stored_facts:
        preference_facts = [row.fact for row in stored_facts if row.category in {'food', 'preference'}]
        work_style_facts = [row.fact for row in stored_facts if row.category == 'work_style']
        update_profile_with_facts(actor, preference_facts=preference_facts, work_style_facts=work_style_facts)

    important_event = extract_important_event(incoming_text)
    if important_event:
        append_event(f'Note from {actor.name}', important_event)

    task_context = _task_context_text(db, actor=actor)
    profile_markdown = profile_text(actor)
    user_directory_text = _user_directory_text(db)
    contact_context = _contact_context_text(
        db,
        actor=actor,
        reply_channel=reply_channel,
        reply_target_id=reply_target_id,
    )
    memory_text = recent_memory_text(db, user_id=actor.id)
    recent_conversation = recent_conversation_text(db, user_id=actor.id)
    events_text = recent_events_text()

    used_llm = False
    try:
        if not is_bot_llm_configured():
            raise BotLLMError('LLM is not configured.')
        message = generate_bot_reply(
            system_prompt=_system_prompt(persona_text()),
            user_prompt=_user_prompt(
                actor=actor,
                incoming_text=incoming_text,
                profile_markdown=profile_markdown,
                user_directory_text=user_directory_text,
                contact_context_text=contact_context,
                memory_text=memory_text,
                recent_conversation=recent_conversation,
                events_text=events_text,
                task_context=task_context,
            ),
        )
        used_llm = True
    except BotLLMError:
        message = _fallback_reply(actor, incoming_text, task_context=task_context, memory_text=memory_text)

    store_conversation_message(
        db,
        user_id=actor.id,
        conversation_id=conversation_id,
        message_id=message_id,
        role='assistant',
        content=message,
        metadata={'source': 'zalo', 'used_llm': used_llm},
    )

    reply = _reply_to_conversation(
        channel=reply_channel,
        target_id=reply_target_id,
        message=message,
        context={'source': 'zalo_chat', 'used_llm': used_llm, 'actor_id': actor.id},
    )
    return BotChatResult(message=message, reply=reply, used_llm=used_llm)
