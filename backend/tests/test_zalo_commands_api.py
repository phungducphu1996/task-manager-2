from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

from sqlalchemy import func, select

from app.bot_llm import BotLLMToolCall, BotLLMToolResponse
from app.config import get_settings
from app.models import BotConversationMessage, BotMemoryFact, Task, TaskPriority, TaskStatus, ZaloIncomingCommand
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


def _tool_response(content: str, tool_calls: list[tuple[str, str, dict]] | None = None) -> BotLLMToolResponse:
    raw_tool_calls = []
    parsed_tool_calls: list[BotLLMToolCall] = []
    for call_id, name, arguments in tool_calls or []:
        raw_tool_calls.append(
            {
                'id': call_id,
                'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)},
            }
        )
        parsed_tool_calls.append(BotLLMToolCall(id=call_id, name=name, arguments=arguments))
    return BotLLMToolResponse(
        content=content,
        tool_calls=parsed_tool_calls,
        assistant_message={'role': 'assistant', 'content': content, 'tool_calls': raw_tool_calls or None},
    )


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


def test_zalo_llm_freeform_create_executes_task(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    _, linh, _ = _users(client)

    monkeypatch.setattr(
        'app.zalo_commands._run_tool_agent',
        lambda *args, **kwargs: {
            'handled': True,
            'action': 'add',
            'message': 'Đã tạo task #1: Chuẩn bị ảnh hero',
        },
    )

    response = client.post(
        '/zalo/incoming',
        json={
            'text': 'Cho bot ghi nhận việc chuẩn bị ảnh hero cho ngày mai',
            'from_uid': linh['zalo_user_id'],
            'conversation_id': linh['zalo_user_id'],
            'conversation_type': 'user',
            'message_id': 'msg-llm-create',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    assert response.json()['action'] == 'add'
    assert replies[-1]['channel'].value == 'user'
    assert 'Chuẩn bị ảnh hero' in replies[-1]['message']


def test_zalo_llm_freeform_create_can_ask_confirm(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    _, linh, _ = _users(client)

    monkeypatch.setattr(
        'app.zalo_commands._run_tool_agent',
        lambda *args, **kwargs: {
            'handled': True,
            'action': 'confirm',
            'message': 'Bạn muốn tôi tạo task "chuẩn bị banner" luôn chứ?',
        },
    )

    response = client.post(
        '/zalo/incoming',
        json={
            'text': 'hay là thêm vụ chuẩn bị banner ha',
            'from_uid': linh['zalo_user_id'],
            'conversation_id': linh['zalo_user_id'],
            'conversation_type': 'user',
            'message_id': 'msg-llm-confirm',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    assert response.json()['action'] == 'confirm'
    assert db_session.scalar(select(func.count(Task.id))) == 0
    assert 'tạo task' in replies[-1]['message']


def test_zalo_llm_freeform_list_executes_list_view(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    _, linh, _ = _users(client)

    monkeypatch.setattr(
        'app.zalo_commands._run_tool_agent',
        lambda *args, **kwargs: {
            'handled': True,
            'action': 'list',
            'message': 'Task today của bạn:\n• #1 Linh today [todo]',
        },
    )

    response = client.post(
        '/zalo/incoming',
        json={
            'text': 'hôm nay em còn task nào chưa xong?',
            'from_uid': linh['zalo_user_id'],
            'conversation_id': linh['zalo_user_id'],
            'conversation_type': 'user',
            'message_id': 'msg-llm-list',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    assert response.json()['action'] == 'list'
    assert 'Linh today' in replies[-1]['message']


def test_zalo_tool_agent_can_approve_review_task(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    admin, linh, _ = _users(client)
    task = Task(
        title='Bluey Collection',
        assigned_to=linh['id'],
        created_by=admin['id'],
        status=TaskStatus.review,
        list_order=1,
    )
    db_session.add(task)
    db_session.commit()

    responses = iter(
        [
            _tool_response('', [('call-1', 'find_tasks', {'query': 'bluey', 'status': 'review', 'limit': 5})]),
            _tool_response('', [('call-2', 'approve_task', {'task_id': task.id})]),
            _tool_response('Đã approve task Bluey Collection sang ready rồi.'),
        ]
    )

    monkeypatch.setattr('app.zalo_commands.is_bot_llm_configured', lambda: True)
    monkeypatch.setattr('app.zalo_commands.complete_bot_conversation', lambda **kwargs: next(responses))

    response = client.post(
        '/zalo/incoming',
        json={
            'text': 'a xác nhận approve bluey nhé',
            'from_uid': admin['zalo_user_id'],
            'conversation_id': admin['zalo_user_id'],
            'conversation_type': 'user',
            'message_id': 'msg-approve-bluey',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    assert response.json()['action'] == 'approve'
    db_session.refresh(task)
    assert task.status == TaskStatus.ready
    assert 'approve task Bluey Collection' in replies[-1]['message']


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


def test_zalo_direct_natural_add_creates_task(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    _, linh, _ = _users(client)

    response = client.post(
        '/zalo/incoming',
        json={
            'text': 'nhớ thêm cho em task làm banner sale ngày mai nha',
            'from_uid': linh['zalo_user_id'],
            'conversation_id': linh['zalo_user_id'],
            'conversation_type': 'user',
            'message_id': 'msg-direct-natural-add',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['action'] == 'add'
    task = db_session.scalar(select(Task).where(Task.id == body['task_id']))
    assert task is not None
    assert task.title == 'banner sale'
    assert task.assigned_to == linh['id']
    assert task.due_date == local_today() + timedelta(days=1)
    assert replies[-1]['channel'].value == 'user'


def test_zalo_group_natural_add_with_alias_creates_task(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    admin, _, _ = _users(client)

    response = client.post(
        '/zalo/incoming',
        json={
            'text': '@TaskBot thêm giúp task chốt layout homepage hôm nay',
            'from_uid': admin['zalo_user_id'],
            'conversation_id': 'test-zalo-group',
            'conversation_type': 'group',
            'message_id': 'msg-group-natural-add',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['action'] == 'add'
    task = db_session.scalar(select(Task).where(Task.id == body['task_id']))
    assert task is not None
    assert task.title == 'chốt layout homepage'
    assert task.due_date == local_today()
    assert replies[-1]['channel'].value == 'group'
    assert replies[-1]['target_id'] == 'test-zalo-group'


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


def test_zalo_plain_question_does_not_create_task(client, db_session, monkeypatch) -> None:
    replies = _install_zalo_reply_stub(monkeypatch)
    _, linh, _ = _users(client)
    monkeypatch.setattr('app.bot_copilot.is_bot_llm_configured', lambda: False)

    response = client.post(
        '/zalo/incoming',
        json={
            'text': 'hôm nay em còn gì cần làm?',
            'from_uid': linh['zalo_user_id'],
            'conversation_id': linh['zalo_user_id'],
            'conversation_type': 'user',
            'message_id': 'msg-plain-question',
        },
        headers=_secret_headers(),
    )

    assert response.status_code == 200
    assert response.json()['action'] == 'chat'
    assert db_session.scalar(select(func.count(Task.id))) == 0
    assert 'chế độ cơ bản' in replies[-1]['message']
