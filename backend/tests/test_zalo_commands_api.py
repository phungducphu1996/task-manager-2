from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.config import get_settings
from app.models import BotConversationMessage, BotMemoryFact, Task, TaskPriority, ZaloIncomingCommand
from app.services import local_today


def _secret_headers() -> dict[str, str]:
    return {'X-Internal-Secret': get_settings().zalo_shared_secret or ''}


def _install_zalo_reply_stub(monkeypatch):
    replies: list[dict] = []

    def _send_zalo_text(**kwargs):
        replies.append(kwargs)
        return True, 200, '{"ok":true}', None

    monkeypatch.setattr('app.zalo_commands.send_zalo_text', _send_zalo_text)
    monkeypatch.setattr('app.bot_copilot.send_zalo_text', _send_zalo_text)
    monkeypatch.setattr('app.notifications._call_worker', lambda payload: (True, 200, '{"ok":true}', None))
    return replies


def _users(client) -> tuple[dict, dict, dict]:
    users = client.get('/users').json()
    admin = next(user for user in users if user['role'] == 'admin')
    linh = next(user for user in users if user['username'] == 'linh')
    quang = next(user for user in users if user['username'] == 'quang')
    return admin, linh, quang


def test_zalo_incoming_requires_secret(client) -> None:
    payload = {
        'text': '@TaskBot list today',
        'from_uid': 'zalo-linh',
        'conversation_id': 'test-zalo-group',
        'conversation_type': 'group',
        'message_id': 'msg-secret',
    }

    missing = client.post('/zalo/incoming', json=payload)
    assert missing.status_code == 401

    wrong = client.post('/zalo/incoming', json=payload, headers={'X-Internal-Secret': 'wrong'})
    assert wrong.status_code == 401


def test_zalo_incoming_ignores_non_commands_and_unallowed_groups(client, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)

    normal_chat = client.post(
        '/zalo/incoming',
        json={
            'text': 'hello team',
            'from_uid': 'zalo-linh',
            'conversation_id': 'test-zalo-group',
            'conversation_type': 'group',
            'message_id': 'msg-normal',
        },
        headers=_secret_headers(),
    )
    assert normal_chat.status_code == 200
    assert normal_chat.json()['ignored'] is True

    wrong_group = client.post(
        '/zalo/incoming',
        json={
            'text': '@TaskBot list today',
            'from_uid': 'zalo-linh',
            'conversation_id': 'other-group',
            'conversation_type': 'group',
            'message_id': 'msg-wrong-group',
        },
        headers=_secret_headers(),
    )
    assert wrong_group.status_code == 200
    assert wrong_group.json()['ignored'] is True
    assert replies == []


def test_zalo_direct_chat_without_alias_replies_to_user(client, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    _, linh, _ = _users(client)
    monkeypatch.setattr('app.bot_copilot.is_bot_llm_configured', lambda: False)

    response = client.post(
        '/zalo/incoming',
        json={
            'text': 'em còn gì cần làm hôm nay?',
            'from_uid': linh['zalo_user_id'],
            'conversation_id': linh['zalo_user_id'],
            'conversation_type': 'direct',
            'message_id': 'msg-direct-chat',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    assert response.json()['action'] == 'chat'
    assert response.json()['used_llm'] is False
    assert replies[-1]['channel'].value == 'user'
    assert replies[-1]['target_id'] == linh['zalo_user_id']


def test_zalo_direct_add_without_alias_creates_task_for_sender(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    _, linh, _ = _users(client)

    response = client.post(
        '/zalo/incoming',
        json={
            'text': 'add Direct task due:today !low',
            'from_uid': linh['zalo_user_id'],
            'conversation_id': linh['zalo_user_id'],
            'conversation_type': 'private',
            'message_id': 'msg-direct-add',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['action'] == 'add'
    task = db_session.scalar(select(Task).where(Task.id == body['task_id']))
    assert task is not None
    assert task.title == 'Direct task'
    assert task.assigned_to == linh['id']
    assert replies[-1]['channel'].value == 'user'
    assert replies[-1]['target_id'] == linh['zalo_user_id']


def test_zalo_add_creates_task_and_dedupes_message_id(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    admin, _, quang = _users(client)

    response = client.post(
        '/zalo/incoming',
        json={
            'text': '@TaskBot add Fix mockup @quang #AmzMage type:Design due:tomorrow !high',
            'from_uid': admin['zalo_user_id'],
            'conversation_id': 'test-zalo-group',
            'conversation_type': 'group',
            'message_id': 'msg-add-1',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    assert body['action'] == 'add'

    task = db_session.scalar(select(Task).where(Task.id == body['task_id']))
    assert task is not None
    assert task.title == '[Design] Fix mockup'
    assert task.assigned_to == quang['id']
    assert task.due_date == local_today() + timedelta(days=1)
    assert task.priority == TaskPriority.high
    assert replies[-1]['channel'].value == 'group'
    assert replies[-1]['target_id'] == 'test-zalo-group'
    assert f'#{task.id}' in replies[-1]['message']

    duplicate = client.post(
        '/zalo/incoming',
        json={
            'text': '@TaskBot add Fix mockup @quang #AmzMage type:Design due:tomorrow !high',
            'from_uid': admin['zalo_user_id'],
            'conversation_id': 'test-zalo-group',
            'conversation_type': 'group',
            'message_id': 'msg-add-1',
        },
        headers=_secret_headers(),
    )
    assert duplicate.status_code == 200
    assert duplicate.json()['duplicate'] is True
    assert db_session.scalar(select(func.count(Task.id)).where(Task.title == '[Design] Fix mockup')) == 1


def test_zalo_add_defaults_assignee_to_sender_for_member(client, db_session, monkeypatch) -> None:
    _install_zalo_reply_stub(monkeypatch)
    _, linh, _ = _users(client)

    response = client.post(
        '/zalo/incoming',
        json={
            'text': '@TaskBot add Member self task due:today !low',
            'from_uid': linh['zalo_user_id'],
            'conversation_id': 'test-zalo-group',
            'conversation_type': 'group',
            'message_id': 'msg-add-self',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    task = db_session.scalar(select(Task).where(Task.id == response.json()['task_id']))
    assert task is not None
    assert task.assigned_to == linh['id']
    assert task.created_by == linh['id']
    assert task.due_date == local_today()
    assert task.priority == TaskPriority.low


def test_zalo_unmapped_sender_replies_error_without_task(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)

    response = client.post(
        '/zalo/incoming',
        json={
            'text': '@TaskBot add Should not create',
            'from_uid': 'unknown-zalo',
            'conversation_id': 'test-zalo-group',
            'conversation_type': 'group',
            'message_id': 'msg-unknown',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    assert response.json()['error'] == 'unmapped_sender'
    assert 'chưa được liên kết' in replies[-1]['message']
    assert db_session.scalar(select(func.count(Task.id)).where(Task.title == 'Should not create')) == 0
    assert db_session.scalar(select(func.count(ZaloIncomingCommand.id))) == 1


def test_zalo_list_today_only_returns_member_tasks(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    admin, linh, quang = _users(client)
    today = local_today()

    db_session.add_all(
        [
            Task(
                title='Linh today',
                assigned_to=linh['id'],
                created_by=admin['id'],
                due_date=today,
                list_order=1,
            ),
            Task(
                title='Quang today',
                assigned_to=quang['id'],
                created_by=admin['id'],
                due_date=today,
                list_order=2,
            ),
        ]
    )
    db_session.commit()

    response = client.post(
        '/zalo/incoming',
        json={
            'text': '@TaskBot list today',
            'from_uid': linh['zalo_user_id'],
            'conversation_id': 'test-zalo-group',
            'conversation_type': 'group',
            'message_id': 'msg-list-today',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    assert response.json()['action'] == 'list'
    reply_text = replies[-1]['message']
    assert 'Linh today' in reply_text
    assert 'Quang today' not in reply_text


def test_zalo_chat_uses_memory_profiles_and_task_context(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    admin, linh, _ = _users(client)
    today = local_today()

    db_session.add(
        Task(
            title='Need today summary',
            assigned_to=linh['id'],
            created_by=admin['id'],
            due_date=today,
            list_order=1,
        )
    )
    db_session.commit()

    captured: dict[str, str] = {}

    def _fake_generate_bot_reply(*, system_prompt: str, user_prompt: str) -> str:
        captured['system'] = system_prompt
        captured['user'] = user_prompt
        return 'Linh ơi, hiện em có 1 task today là "Need today summary". Em cũng nhớ là em thích trà sữa.'

    monkeypatch.setattr('app.bot_copilot.is_bot_llm_configured', lambda: True)
    monkeypatch.setattr('app.bot_copilot.generate_bot_reply', _fake_generate_bot_reply)

    response = client.post(
        '/zalo/incoming',
        json={
            'text': '@TaskBot em thích trà sữa và hôm nay còn task gì không?',
            'from_uid': linh['zalo_user_id'],
            'conversation_id': 'test-zalo-group',
            'conversation_type': 'group',
            'message_id': 'msg-chat-1',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    assert body['action'] == 'chat'
    assert body['used_llm'] is True
    assert 'Need today summary' in captured['user']
    assert 'Profile markdown' in captured['user']
    assert 'Known memory facts' in captured['user']
    assert replies[-1]['message'].startswith('Linh ơi')

    stored_messages = db_session.scalars(
        select(BotConversationMessage).where(BotConversationMessage.user_id == linh['id'])
    ).all()
    assert len(stored_messages) == 2
    assert {row.role for row in stored_messages} == {'user', 'assistant'}

    facts = db_session.scalars(select(BotMemoryFact).where(BotMemoryFact.user_id == linh['id'])).all()
    assert len(facts) == 1
    assert 'thích trà sữa' in facts[0].fact

    settings = get_settings()
    profile_dir = Path(settings.resolve_runtime_path(settings.bot_profiles_dir))
    profile_files = list(profile_dir.glob('*.md'))
    assert profile_files
    profile_text = profile_files[0].read_text(encoding='utf-8')
    assert 'thích trà sữa' in profile_text


def test_zalo_chat_falls_back_without_llm(client, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    _, linh, _ = _users(client)
    monkeypatch.setattr('app.bot_copilot.is_bot_llm_configured', lambda: False)

    response = client.post(
        '/zalo/incoming',
        json={
            'text': '@TaskBot em còn gì cần làm hôm nay?',
            'from_uid': linh['zalo_user_id'],
            'conversation_id': 'test-zalo-group',
            'conversation_type': 'group',
            'message_id': 'msg-chat-fallback',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    assert response.json()['action'] == 'chat'
    assert response.json()['used_llm'] is False
    assert 'chế độ cơ bản' in replies[-1]['message']
