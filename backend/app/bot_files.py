from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

from .config import get_settings
from .models import User

settings = get_settings()

DEFAULT_PERSONA = """# Hazel Office Bot

Bạn là trợ lý nội bộ của văn phòng Hazel.

## Tính cách
- Ấm áp, nhanh nhạy, thân thiện, nói chuyện tự nhiên.
- Trả lời ngắn gọn trước, đủ ý, không khoa trương.
- Biết nhắc việc, biết quan tâm, nhưng không phán xét.

## Nguyên tắc
- Luôn ưu tiên tiếng Việt tự nhiên.
- Khi trả lời về task, bám sát dữ liệu hiện tại, không bịa.
- Nếu chưa chắc, nói rõ điều gì chưa chắc.
- Nếu người hỏi là admin, có thể nói về toàn team trong phạm vi dữ liệu cho phép.
- Nếu người hỏi là member, ưu tiên task và context liên quan đến chính họ.

## Vai trò
- Hỗ trợ hỏi đáp về task, công việc, văn phòng, thói quen làm việc.
- Ghi nhớ dần sở thích, thói quen, thông tin hữu ích của từng người.
- Có thể nhắc lại bối cảnh gần đây để trả lời tự nhiên hơn.
"""

DEFAULT_EVENTS = """# Office Events

Các sự kiện đáng nhớ của văn phòng sẽ được append ở đây.
"""


def _resolve(raw_path: str) -> Path:
    return settings.resolve_runtime_path(raw_path)


def ensure_bot_files() -> None:
    persona_path = _resolve(settings.bot_persona_path)
    persona_path.parent.mkdir(parents=True, exist_ok=True)
    if not persona_path.exists():
        persona_path.write_text(DEFAULT_PERSONA, encoding='utf-8')

    profiles_dir = _resolve(settings.bot_profiles_dir)
    profiles_dir.mkdir(parents=True, exist_ok=True)

    events_path = _resolve(settings.bot_events_path)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    if not events_path.exists():
        events_path.write_text(DEFAULT_EVENTS, encoding='utf-8')


def persona_text() -> str:
    ensure_bot_files()
    return _resolve(settings.bot_persona_path).read_text(encoding='utf-8').strip()


def user_profile_path(user: User) -> Path:
    ensure_bot_files()
    slug = (user.username or user.id or 'unknown').strip().replace('/', '-')
    return _resolve(settings.bot_profiles_dir) / f'{slug}.md'


def ensure_user_profile(user: User) -> Path:
    path = user_profile_path(user)
    if path.exists():
        return path
    path.write_text(
        (
            f'# Profile: {user.name}\n\n'
            '## Identity\n'
            f'- Name: {user.name}\n'
            f'- Username: {user.username}\n'
            f'- Role: {user.role or "unknown"}\n'
            f'- User ID: {user.id}\n'
            f'- Zalo User ID: {user.zalo_user_id or "unknown"}\n\n'
            '## Preferences\n'
            '- Chưa có dữ liệu.\n\n'
            '## Work Style\n'
            '- Chưa có dữ liệu.\n\n'
            '## Notes\n'
            '- Hồ sơ này sẽ được bot cập nhật dần theo hội thoại.\n'
        ),
        encoding='utf-8',
    )
    return path


def profile_text(user: User) -> str:
    path = ensure_user_profile(user)
    return path.read_text(encoding='utf-8').strip()


def _insert_bullets(content: str, heading: str, bullets: Iterable[str]) -> str:
    bullet_lines = [f'- {bullet.strip()}' for bullet in bullets if bullet.strip()]
    if not bullet_lines:
        return content

    marker = f'## {heading}\n'
    if marker not in content:
        suffix = '\n'.join([marker, *bullet_lines, ''])
        return content.rstrip() + '\n\n' + suffix

    before, after = content.split(marker, 1)
    next_heading_index = after.find('\n## ')
    if next_heading_index == -1:
        section = after
        tail = ''
    else:
        section = after[:next_heading_index]
        tail = after[next_heading_index:]

    existing = {line.strip() for line in section.splitlines() if line.strip().startswith('- ')}
    merged = section.rstrip()
    for line in bullet_lines:
        if line not in existing:
            merged += f'\n{line}'

    return before + marker + merged.strip() + tail


def update_profile_with_facts(user: User, *, preference_facts: Iterable[str], work_style_facts: Iterable[str]) -> None:
    path = ensure_user_profile(user)
    content = path.read_text(encoding='utf-8')
    content = _insert_bullets(content, 'Preferences', preference_facts)
    content = _insert_bullets(content, 'Work Style', work_style_facts)
    path.write_text(content.strip() + '\n', encoding='utf-8')


def append_event(title: str, content: str) -> None:
    ensure_bot_files()
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'\n## {timestamp} - {title}\n- {content.strip()}\n'
    path = _resolve(settings.bot_events_path)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(line)


def recent_events_text(limit: int = 8) -> str:
    ensure_bot_files()
    lines = _resolve(settings.bot_events_path).read_text(encoding='utf-8').splitlines()
    if len(lines) <= 2:
        return 'Chưa có sự kiện văn phòng nào được lưu.'
    return '\n'.join(lines[-limit * 2 :]).strip()
