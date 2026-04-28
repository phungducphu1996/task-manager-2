from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import BotConversationMessage, BotConversationState, BotMemoryFact, User

settings = get_settings()


@dataclass(slots=True)
class ExtractedMemory:
    category: str
    fact: str
    confidence: int


FOOD_HINTS = ('ăn', 'uống', 'cafe', 'trà', 'trà sữa', 'cơm', 'bún', 'phở', 'đồ ngọt', 'cay')
PREFERENCE_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ('food', re.compile(r'\b(?:anh|chị|em|mình|tôi)?\s*(?:thích|hay ăn|hay uống)\s+(.+)', re.I), 80),
    ('food', re.compile(r'\b(?:anh|chị|em|mình|tôi)?\s*(?:không ăn|không uống|ghét)\s+(.+)', re.I), 85),
    ('food', re.compile(r'\b(?:anh|chị|em|mình|tôi)?\s*(?:dị ứng)\s+(.+)', re.I), 90),
    ('work_style', re.compile(r'\b(?:anh|chị|em|mình|tôi)?\s*(?:hay|thường)\s+(.+)', re.I), 65),
    ('preference', re.compile(r'\b(?:anh|chị|em|mình|tôi)?\s*(?:muốn|ưu tiên)\s+(.+)', re.I), 70),
]
IMPORTANT_EVENT_PATTERNS = [
    re.compile(r'\b(?:nhớ|ghi nhớ|quan trọng là|lưu ý là)\b(.+)', re.I),
    re.compile(r'\b(?:sinh nhật|nghỉ phép|đi công tác|họp toàn team)\b(.+)', re.I),
]


def store_conversation_message(
    db: Session,
    *,
    user_id: str | None,
    conversation_id: str | None,
    message_id: str | None,
    role: str,
    content: str,
    metadata: dict | None = None,
) -> BotConversationMessage:
    message = BotConversationMessage(
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        role=role,
        content=content.strip(),
        metadata_json=metadata or {},
    )
    db.add(message)
    db.flush()
    _trim_recent_messages(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
        limit=settings.bot_recent_conversation_limit,
    )
    return message


def _trim_recent_messages(
    db: Session,
    *,
    user_id: str | None,
    conversation_id: str | None = None,
    limit: int,
) -> None:
    if conversation_id:
        ids = db.scalars(
            select(BotConversationMessage.id)
            .where(BotConversationMessage.conversation_id == conversation_id)
            .order_by(BotConversationMessage.created_at.desc(), BotConversationMessage.id.desc())
            .offset(limit)
        ).all()
        if ids:
            db.execute(delete(BotConversationMessage).where(BotConversationMessage.id.in_(ids)))
        return

    if not user_id:
        return
    ids = db.scalars(
        select(BotConversationMessage.id)
        .where(BotConversationMessage.user_id == user_id)
        .order_by(BotConversationMessage.created_at.desc(), BotConversationMessage.id.desc())
        .offset(limit)
    ).all()
    if ids:
        db.execute(delete(BotConversationMessage).where(BotConversationMessage.id.in_(ids)))


def recent_conversation_text(
    db: Session,
    *,
    user_id: str | None = None,
    conversation_id: str | None = None,
    limit: int | None = None,
) -> str:
    actual_limit = limit or settings.bot_recent_conversation_limit
    if not user_id and not conversation_id:
        return 'Chưa có lịch sử hội thoại.'

    conditions = []
    if conversation_id:
        conditions.append(BotConversationMessage.conversation_id == conversation_id)
    elif user_id:
        conditions.append(BotConversationMessage.user_id == user_id)

    rows = db.scalars(
        select(BotConversationMessage)
        .where(*conditions)
        .order_by(BotConversationMessage.created_at.desc(), BotConversationMessage.id.desc())
        .limit(actual_limit)
    ).all()
    if not rows:
        return 'Chưa có lịch sử hội thoại.'
    ordered = list(reversed(rows))
    return '\n'.join(f'{row.role}: {row.content}' for row in ordered)


def get_conversation_state(
    db: Session,
    *,
    user_id: str | None,
    conversation_id: str | None,
) -> BotConversationState | None:
    if not conversation_id:
        return None
    return db.scalar(select(BotConversationState).where(BotConversationState.conversation_id == conversation_id))


def conversation_state_text(state: BotConversationState | None) -> str:
    if not state or not state.state_json:
        return 'Chưa có state đang mở.'

    data = state.state_json or {}
    lines: list[str] = []
    active_task = data.get('active_task') or {}
    if active_task:
        task_id = active_task.get('id')
        title = active_task.get('title')
        status = active_task.get('status')
        assignee = active_task.get('assignee')
        due_date = active_task.get('due_date')
        details = [f'id={task_id}', f'title={title}']
        if status:
            details.append(f'status={status}')
        if assignee:
            details.append(f'assignee={assignee}')
        if due_date:
            details.append(f'due={due_date}')
        lines.append(f'Active task: {" | ".join(details)}')

    pending = data.get('pending_intent') or {}
    if pending:
        fields = pending.get('fields') or {}
        lines.append(
            'Pending intent: '
            f'action={pending.get("action")}; task_id={pending.get("task_id")}; fields={fields}; '
            f'needs_confirmation={pending.get("needs_confirmation", False)}'
        )

    failures = data.get('last_failed_tool_results') or []
    if failures:
        errors = [str(item.get('error') or item) for item in failures[:3]]
        lines.append(f'Last failed tool results: {"; ".join(errors)}')

    successes = data.get('last_successful_tool_results') or []
    if successes:
        names = [str(item.get('name') or 'tool') for item in successes[:3]]
        lines.append(f'Last successful tools: {", ".join(names)}')

    return '\n'.join(lines) if lines else 'Chưa có state đang mở.'


def upsert_conversation_state(
    db: Session,
    *,
    user_id: str | None,
    conversation_id: str | None,
    state_data: dict,
) -> BotConversationState | None:
    if not conversation_id:
        return None
    state = get_conversation_state(db, user_id=user_id, conversation_id=conversation_id)
    if state is None:
        state = BotConversationState(user_id=user_id, conversation_id=conversation_id, state_json=state_data)
        db.add(state)
        db.flush()
        return state
    state.user_id = user_id or state.user_id
    state.state_json = state_data
    db.add(state)
    db.flush()
    return state


def extract_memory_facts(text: str) -> list[ExtractedMemory]:
    cleaned = ' '.join(text.strip().split())
    if not cleaned:
        return []

    matches: list[ExtractedMemory] = []
    lower = cleaned.casefold()

    for category, pattern, confidence in PREFERENCE_PATTERNS:
        found = pattern.search(cleaned)
        if not found:
            continue
        fact = found.group(0).strip().rstrip('.!?')
        if category == 'food' and not any(hint in lower for hint in FOOD_HINTS):
            continue
        if len(fact) < 6 or len(fact) > 180:
            continue
        matches.append(ExtractedMemory(category=category, fact=fact, confidence=confidence))

    unique: dict[tuple[str, str], ExtractedMemory] = {}
    for item in matches:
        key = (item.category, item.fact.casefold())
        unique[key] = item
    return list(unique.values())


def upsert_memory_facts(
    db: Session,
    *,
    user: User,
    source_message_id: str | None,
    facts: list[ExtractedMemory],
) -> list[BotMemoryFact]:
    saved: list[BotMemoryFact] = []
    for fact in facts:
        existing = db.scalar(
            select(BotMemoryFact).where(
                BotMemoryFact.user_id == user.id,
                BotMemoryFact.category == fact.category,
                BotMemoryFact.fact == fact.fact,
            )
        )
        if existing:
            existing.confidence = max(existing.confidence, fact.confidence)
            existing.source_message_id = source_message_id or existing.source_message_id
            saved.append(existing)
            continue

        row = BotMemoryFact(
            user_id=user.id,
            category=fact.category,
            fact=fact.fact,
            confidence=fact.confidence,
            source_message_id=source_message_id,
        )
        db.add(row)
        db.flush()
        saved.append(row)
    return saved


def recent_memory_text(db: Session, *, user_id: str | None, limit: int | None = None) -> str:
    actual_limit = limit or settings.bot_memory_facts_limit
    if not user_id:
        return 'Chưa có memory facts.'
    rows = db.scalars(
        select(BotMemoryFact)
        .where(BotMemoryFact.user_id == user_id)
        .order_by(BotMemoryFact.updated_at.desc(), BotMemoryFact.id.desc())
        .limit(actual_limit)
    ).all()
    if not rows:
        return 'Chưa có memory facts.'
    return '\n'.join(f'- [{row.category}] {row.fact}' for row in rows)


def extract_important_event(text: str) -> str | None:
    cleaned = ' '.join(text.strip().split())
    if not cleaned:
        return None
    for pattern in IMPORTANT_EVENT_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            snippet = match.group(0).strip().rstrip('.!?')
            if 6 <= len(snippet) <= 180:
                return snippet
    return None
