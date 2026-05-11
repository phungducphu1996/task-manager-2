from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
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

DEFAULT_CONTACTS = """# Hazel Contact Registry

File này được sync từ database user và env group của Task Manager.
Bạn có thể chỉnh các file custom prompt được link ở từng contact; registry này có thể được ghi lại khi bot sync.
"""

DEFAULT_NOTIFICATION_PROMPT = """# Hazel Notification Writer

Bạn viết thông báo Zalo ngắn cho Task Manager của văn phòng Hazel.

## Giọng văn
- Tiếng Việt tự nhiên, ấm, lanh, hơi vui nhưng không lố.
- Xưng "em" khi phù hợp, gọi người nhận theo tên.
- Không dài dòng. Mục tiêu là người nhận hiểu ngay cần làm gì.

## Quy tắc
- Bám sát JSON event được đưa vào, không bịa task, không bịa người.
- Giữ thông báo 1-4 dòng.
- Nếu là task mới: nói rõ người nhận vừa được giao task.
- Nếu task được sửa: nói rõ các field chính vừa đổi, nếu có.
- Nếu task bị xoá: nói rõ task đã bị xoá bởi ai.
- Nếu review/approve/done: nói rõ trạng thái mới và hành động mong muốn.
- Nếu JSON event có task.url, luôn giữ link ở cuối theo format: `Link task: <url>`.
- Có thể dùng emoji rất ít, tối đa 1 emoji nếu hợp.
"""

DEFAULT_NOTIFICATION_EVENT_PROMPT = """# Event-specific Notification Prompt

File này áp dụng thêm cho riêng một loại notification.

## Cách dùng
- Ghi tone/cấu trúc riêng cho event này.
- Không cần lặp lại toàn bộ prompt global.
- Nếu để trống, bot chỉ dùng prompt global.
"""


def _resolve(raw_path: str) -> Path:
    return settings.resolve_runtime_path(raw_path)


def _slug(value: str) -> str:
    normalized = re.sub(r'[^A-Za-z0-9._-]+', '-', value.strip())
    return normalized.strip('-') or 'unknown'


def ensure_bot_files() -> None:
    persona_path = _resolve(settings.bot_persona_path)
    persona_path.parent.mkdir(parents=True, exist_ok=True)
    if not persona_path.exists():
        persona_path.write_text(DEFAULT_PERSONA, encoding='utf-8')

    notification_prompt_path = _resolve(settings.bot_notification_prompt_path)
    notification_prompt_path.parent.mkdir(parents=True, exist_ok=True)
    if not notification_prompt_path.exists():
        notification_prompt_path.write_text(DEFAULT_NOTIFICATION_PROMPT, encoding='utf-8')
    (notification_prompt_path.parent / 'notification-events').mkdir(parents=True, exist_ok=True)

    profiles_dir = _resolve(settings.bot_profiles_dir)
    profiles_dir.mkdir(parents=True, exist_ok=True)

    contacts_path = _resolve(settings.bot_contacts_path)
    contacts_path.parent.mkdir(parents=True, exist_ok=True)
    if not contacts_path.exists():
        contacts_path.write_text(DEFAULT_CONTACTS, encoding='utf-8')

    contact_prompts_dir = _resolve(settings.bot_contact_prompts_dir)
    (contact_prompts_dir / 'personal').mkdir(parents=True, exist_ok=True)
    (contact_prompts_dir / 'groups').mkdir(parents=True, exist_ok=True)

    events_path = _resolve(settings.bot_events_path)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    if not events_path.exists():
        events_path.write_text(DEFAULT_EVENTS, encoding='utf-8')


def persona_text() -> str:
    ensure_bot_files()
    return _resolve(settings.bot_persona_path).read_text(encoding='utf-8').strip()


def notification_prompt_text() -> str:
    ensure_bot_files()
    return _resolve(settings.bot_notification_prompt_path).read_text(encoding='utf-8').strip()


def notification_event_prompt_path(event_type: str) -> Path:
    ensure_bot_files()
    filename = f'{_slug(event_type)}.md'
    return _resolve(settings.bot_notification_prompt_path).parent / 'notification-events' / filename


def ensure_notification_event_prompt(event_type: str) -> Path:
    path = notification_event_prompt_path(event_type)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_NOTIFICATION_EVENT_PROMPT, encoding='utf-8')
    return path


def notification_event_prompt_text(event_type: str, *, max_chars: int = 2200) -> str:
    if not event_type:
        return ''
    path = notification_event_prompt_path(event_type)
    if not path.exists():
        return ''
    content = path.read_text(encoding='utf-8').strip()
    if content == DEFAULT_NOTIFICATION_EVENT_PROMPT.strip():
        return ''
    return content[:max_chars]


def user_contact_prompt_path(user: User) -> Path:
    ensure_bot_files()
    filename = f'{_slug(user.username or user.id or user.name)}.md'
    return _resolve(settings.bot_contact_prompts_dir) / 'personal' / filename


def group_contact_prompt_path(group_id: str, group_name: str | None = None) -> Path:
    ensure_bot_files()
    filename = f'{_slug(group_name or group_id)}.md'
    return _resolve(settings.bot_contact_prompts_dir) / 'groups' / filename


def ensure_user_contact_prompt(user: User) -> Path:
    path = user_contact_prompt_path(user)
    if path.exists():
        return path
    path.write_text(
        (
            f'# Custom Prompt: {user.name}\n\n'
            '## Identity\n'
            f'- Name: {user.name}\n'
            f'- Username: {user.username}\n'
            f'- Role: {user.role or "unknown"}\n'
            f'- User ID: {user.id}\n'
            f'- Zalo User ID: {user.zalo_user_id or "unknown"}\n\n'
            '## Aliases\n'
            '- Chưa có biệt danh.\n\n'
            '## How to Talk to This Person\n'
            '- Chưa có custom riêng.\n\n'
            '## Notification Style\n'
            '- Ngắn gọn, rõ việc cần làm.\n'
        ),
        encoding='utf-8',
    )
    return path


def ensure_group_contact_prompt(group_id: str, group_name: str) -> Path:
    path = group_contact_prompt_path(group_id, group_name)
    if path.exists():
        return path
    path.write_text(
        (
            f'# Custom Prompt: {group_name}\n\n'
            '## Identity\n'
            f'- Group Name: {group_name}\n'
            f'- Group ID: {group_id}\n\n'
            '## How to Talk in This Group\n'
            '- Nói gọn, tự nhiên, hợp văn phòng.\n'
            '- Nếu nhắc task chung, ưu tiên rõ người chịu trách nhiệm và deadline.\n\n'
            '## Notification Style\n'
            '- Có thể thân mật hơn chat riêng, nhưng không spam.\n'
        ),
        encoding='utf-8',
    )
    return path


def contact_prompt_text_for_user(user: User, *, max_chars: int = 1800) -> str:
    path = ensure_user_contact_prompt(user)
    return path.read_text(encoding='utf-8').strip()[:max_chars]


def user_contact_aliases(user: User) -> list[str]:
    path = ensure_user_contact_prompt(user)
    content = path.read_text(encoding='utf-8')
    aliases: list[str] = []
    in_aliases = False
    alias_headings = {
        'aliases',
        'alias',
        'allias',
        'alliases',
        'biệt danh',
        'biet danh',
        'nicknames',
        'nickname',
    }
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith('## '):
            heading = line.removeprefix('##').strip().casefold()
            in_aliases = heading in alias_headings
            continue
        if not line.startswith('- '):
            continue
        value = line[2:].strip()
        if not in_aliases:
            lower_value = value.casefold()
            if lower_value.startswith(('alias:', 'aliases:', 'allias:', 'alliases:', 'biệt danh:', 'biet danh:')):
                value = value.split(':', 1)[1].strip()
            else:
                continue
        if not value or value.casefold().startswith('chưa có') or value.casefold().startswith('chua co'):
            continue
        aliases.append(value)
    return aliases


def contact_prompt_text_for_group(group_id: str, group_name: str | None = None, *, max_chars: int = 1800) -> str:
    path = ensure_group_contact_prompt(group_id, group_name or group_id)
    return path.read_text(encoding='utf-8').strip()[:max_chars]


def sync_contact_registry(users: Iterable[User], group_entries: Iterable[tuple[str, str]]) -> str:
    ensure_bot_files()
    sorted_users = sorted(users, key=lambda user: ((user.name or '').casefold(), (user.username or '').casefold()))
    sorted_groups = sorted(group_entries, key=lambda item: (item[1].casefold(), item[0]))

    lines = [
        '# Hazel Contact Registry',
        '',
        'File này được sync từ database user và env group của Task Manager.',
        'Custom prompt riêng nằm ở từng file được link bên dưới; bot sẽ đọc các file đó khi chat hoặc gửi notification.',
        '',
        '## Personal Contacts',
        '',
    ]
    if not sorted_users:
        lines.append('- Chưa có user active nào.')
    for user in sorted_users:
        prompt_path = ensure_user_contact_prompt(user)
        relative_prompt = prompt_path.relative_to(_resolve('.')) if prompt_path.is_relative_to(_resolve('.')) else prompt_path
        lines.extend(
            [
                f'### {user.name}',
                f'- Type: personal',
                f'- User ID: {user.id}',
                f'- Username: {user.username}',
                f'- Role: {user.role or "unknown"}',
                f'- Zalo User ID: {user.zalo_user_id or "unknown"}',
                f'- Aliases: {", ".join(user_contact_aliases(user)) or "none"}',
                f'- Custom Prompt File: {relative_prompt}',
                '',
            ]
        )

    lines.extend(['## Group Contacts', ''])
    if not sorted_groups:
        lines.append('- Chưa cấu hình group nào trong ZALO_GROUP_ID/ZALO_ALLOWED_GROUP_IDS.')
    for group_id, group_name in sorted_groups:
        prompt_path = ensure_group_contact_prompt(group_id, group_name)
        relative_prompt = prompt_path.relative_to(_resolve('.')) if prompt_path.is_relative_to(_resolve('.')) else prompt_path
        lines.extend(
            [
                f'### {group_name}',
                f'- Type: group',
                f'- Group ID: {group_id}',
                f'- Custom Prompt File: {relative_prompt}',
                '',
            ]
        )

    content = '\n'.join(lines).rstrip() + '\n'
    _resolve(settings.bot_contacts_path).write_text(content, encoding='utf-8')
    return content


def contact_registry_text(users: Iterable[User], group_entries: Iterable[tuple[str, str]], *, max_chars: int = 6000) -> str:
    return sync_contact_registry(users, group_entries).strip()[:max_chars]


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


def profile_summary_text(user: User, *, max_lines: int = 8) -> str:
    content = profile_text(user)
    lines = [line.rstrip() for line in content.splitlines() if line.strip()]
    return '\n'.join(lines[:max_lines]).strip()


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
