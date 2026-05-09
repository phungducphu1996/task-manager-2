from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.models import NotificationChannel, NotificationDelivery, NotificationEvent, NotificationStatus, Task, TaskStatus
from app.notifications import NotificationSpec, dispatch_due_notification_events, enqueue_notification_event


def actor_headers(user_id: str) -> dict[str, str]:
    return {'X-Actor-Id': user_id}


def select_users(users: list[dict]) -> tuple[dict, dict, dict]:
    admin = next(user for user in users if user['role'] == 'admin')
    members = [user for user in users if user['role'] != 'admin']
    return admin, members[0], members[1]


def install_worker_success_stub(monkeypatch):
    calls: list[dict] = []

    def _success(payload: dict):
        calls.append(payload)
        return True, 200, '{"ok":true}', None

    monkeypatch.setattr('app.notifications._call_worker', _success)
    return calls


def test_realtime_create_assignment_notification(client, db_session, monkeypatch) -> None:
    calls = install_worker_success_stub(monkeypatch)

    users = client.get('/users').json()
    admin, member, _ = select_users(users)
    created = client.post(
        '/tasks',
        json={
            'title': 'Realtime assign',
            'assigned_to': member['id'],
            'created_by': admin['id'],
        },
        headers=actor_headers(admin['id']),
    )
    assert created.status_code == 201

    event = db_session.scalar(
        select(NotificationEvent).where(NotificationEvent.event_type == 'task_assigned_on_create')
    )
    assert event is not None
    assert event.status == NotificationStatus.sent
    assert event.channel == NotificationChannel.user
    assert event.target_id == member['zalo_user_id']

    deliveries = db_session.scalars(
        select(NotificationDelivery).where(NotificationDelivery.event_id == event.id)
    ).all()
    assert len(deliveries) == 1
    assert calls and calls[0]['channel'] == 'user'


def test_realtime_notification_message_can_be_rendered_by_llm(client, db_session, monkeypatch) -> None:
    install_worker_success_stub(monkeypatch)
    settings = get_settings()
    settings.openai_api_key = 'test-openai-key'
    prompt_path = Path(settings.bot_notification_prompt_path)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text('# Custom Notify Voice\nNói chuyện duyên dáng kiểu Hazel.', encoding='utf-8')
    event_prompt_path = prompt_path.parent / 'notification-events' / 'task_assigned_on_create.md'
    event_prompt_path.parent.mkdir(parents=True, exist_ok=True)
    event_prompt_path.write_text('# Assigned Voice\nNhấn mạnh đây là task mới được giao.', encoding='utf-8')

    captured: dict[str, str] = {}

    def _fake_generate_bot_reply(*, system_prompt: str, user_prompt: str) -> str:
        captured['system_prompt'] = system_prompt
        captured['user_prompt'] = user_prompt
        return 'LLM: task mới tới rồi, xử lý nhẹ nhàng nha.'

    monkeypatch.setattr('app.notifications.generate_bot_reply', _fake_generate_bot_reply)

    users = client.get('/users').json()
    admin, member, _ = select_users(users)
    prompt_dir = Path(settings.bot_contact_prompts_dir) / 'personal'
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / f'{member["username"]}.md').write_text(
        '# Custom Prompt: member\nNói với member bằng giọng cực ngắn.',
        encoding='utf-8',
    )
    created = client.post(
        '/tasks',
        json={
            'title': 'LLM assign',
            'assigned_to': member['id'],
            'created_by': admin['id'],
        },
        headers=actor_headers(admin['id']),
    )

    assert created.status_code == 201
    event = db_session.scalar(
        select(NotificationEvent).where(NotificationEvent.event_type == 'task_assigned_on_create')
    )
    assert event is not None
    assert event.payload['message'] == 'LLM: task mới tới rồi, xử lý nhẹ nhàng nha.'
    assert event.payload['context']['llm_rendered'] is True
    assert 'Custom Notify Voice' in captured['system_prompt']
    assert 'Assigned Voice' in captured['system_prompt']
    assert 'LLM assign' in captured['user_prompt']
    assert 'Nói với member bằng giọng cực ngắn.' in captured['user_prompt']


def test_realtime_status_transition_notifications(client, db_session, monkeypatch) -> None:
    install_worker_success_stub(monkeypatch)

    users = client.get('/users').json()
    admin, member, _ = select_users(users)
    created = client.post(
        '/tasks',
        json={'title': 'Status flow', 'assigned_to': member['id'], 'created_by': admin['id']},
        headers=actor_headers(admin['id']),
    )
    assert created.status_code == 201
    task_id = created.json()['id']

    to_review = client.patch(
        f'/tasks/{task_id}/status',
        json={'status': 'review'},
        headers=actor_headers(member['id']),
    )
    assert to_review.status_code == 200

    same_review = client.patch(
        f'/tasks/{task_id}/status',
        json={'status': 'review'},
        headers=actor_headers(member['id']),
    )
    assert same_review.status_code == 200

    to_ready = client.patch(
        f'/tasks/{task_id}/status',
        json={'status': 'ready'},
        headers=actor_headers(admin['id']),
    )
    assert to_ready.status_code == 200

    to_done = client.patch(
        f'/tasks/{task_id}/status',
        json={'status': 'done'},
        headers=actor_headers(member['id']),
    )
    assert to_done.status_code == 200

    task_events = db_session.scalars(
        select(NotificationEvent).where(NotificationEvent.task_id == task_id)
    ).all()
    by_type = {}
    for event in task_events:
        by_type.setdefault(event.event_type, 0)
        by_type[event.event_type] += 1

    assert by_type.get('task_submitted_for_review', 0) == 1
    assert by_type.get('task_approved_ready', 0) == 1
    assert by_type.get('task_done_by_member', 0) == 1


def test_realtime_update_and_delete_notifications(client, db_session, monkeypatch) -> None:
    install_worker_success_stub(monkeypatch)

    users = client.get('/users').json()
    admin, member, _ = select_users(users)
    created = client.post(
        '/tasks',
        json={'title': 'Mutable task', 'assigned_to': member['id'], 'created_by': admin['id']},
        headers=actor_headers(admin['id']),
    )
    assert created.status_code == 201
    task_id = created.json()['id']

    updated = client.patch(
        f'/tasks/{task_id}',
        json={'due_date': '2026-04-30'},
        headers=actor_headers(admin['id']),
    )
    assert updated.status_code == 200

    deleted = client.delete(f'/tasks/{task_id}', headers=actor_headers(admin['id']))
    assert deleted.status_code == 204

    updated_event = db_session.scalar(
        select(NotificationEvent).where(NotificationEvent.event_type == 'task_updated')
    )
    deleted_event = db_session.scalar(
        select(NotificationEvent).where(NotificationEvent.event_type == 'task_deleted')
    )

    assert updated_event is not None
    assert updated_event.target_id == member['zalo_user_id']
    assert updated_event.payload['context']['changed_fields'] == ['due_date']
    assert deleted_event is not None
    assert deleted_event.target_id == member['zalo_user_id']
    assert deleted_event.payload['context']['task_id'] == task_id


def test_internal_notification_job_requires_token(client) -> None:
    missing = client.post('/internal/notifications/run?job=morning')
    assert missing.status_code == 401

    wrong = client.post('/internal/notifications/run?job=morning', headers={'X-Internal-Token': 'wrong-token'})
    assert wrong.status_code == 403


def test_admin_notification_ui_and_status(client) -> None:
    users = client.get('/users').json()
    admin, member, _ = select_users(users)

    page = client.get('/admin/notifications/ui')
    assert page.status_code == 200
    assert 'Noti Control' in page.text

    forbidden = client.get('/admin/notifications/status', headers=actor_headers(member['id']))
    assert forbidden.status_code == 403

    status_response = client.get('/admin/notifications/status', headers=actor_headers(admin['id']))
    assert status_response.status_code == 200
    data = status_response.json()
    assert data['config']['zalo_worker_configured'] is True
    assert data['notification_counts']['pending'] == 0
    assert 'reminder_counts' in data
    assert data['contacts']['groups'][0]['group_id'] == 'test-zalo-group'
    assert any(user['zalo_user_id'] == admin['zalo_user_id'] for user in data['contacts']['users'])
    assert data['cron_health']['vikunja_reconcile_running'] is False
    assert data['cron_health']['timezone'] == 'Asia/Ho_Chi_Minh'
    assert 'vikunja_task_assigned' in data['event_prompt_types']
    assert 'daily_group_digest' in data['core_rule_summary']
    assert data['scheduler_plan']['timer_name'] == 'taskmanager-reminder-tick.timer'


def test_admin_notification_test_endpoint_sends_zalo(client, monkeypatch) -> None:
    calls = install_worker_success_stub(monkeypatch)
    users = client.get('/users').json()
    admin, _, _ = select_users(users)

    response = client.post(
        '/admin/notifications/test',
        json={'channel': 'group', 'target_id': 'test-zalo-group', 'message': 'Ping noti UI'},
        headers=actor_headers(admin['id']),
    )

    assert response.status_code == 200
    assert response.json()['ok'] is True
    assert calls == [
        {
            'channel': 'group',
            'target_id': 'test-zalo-group',
            'message': 'Ping noti UI',
            'context': {'source': 'admin_notification_ui'},
        }
    ]


def test_admin_notification_prompt_can_be_updated(client) -> None:
    users = client.get('/users').json()
    admin, member, _ = select_users(users)

    forbidden = client.put(
        '/admin/notifications/prompt',
        json={'content': '# Nope'},
        headers=actor_headers(member['id']),
    )
    assert forbidden.status_code == 403

    updated = client.put(
        '/admin/notifications/prompt',
        json={'content': '# Notify Voice\nNói cực gọn và vui.'},
        headers=actor_headers(admin['id']),
    )
    assert updated.status_code == 200
    assert updated.json()['bytes'] > 0

    loaded = client.get('/admin/notifications/prompt', headers=actor_headers(admin['id']))
    assert loaded.status_code == 200
    assert 'Nói cực gọn và vui.' in loaded.json()['content']

    event_updated = client.put(
        '/admin/notifications/prompt?event_type=vikunja_task_assigned',
        json={'content': '# Assigned\nNói thân mật hơn khi giao task.'},
        headers=actor_headers(admin['id']),
    )
    assert event_updated.status_code == 200
    assert event_updated.json()['scope'] == 'vikunja_task_assigned'

    event_loaded = client.get(
        '/admin/notifications/prompt?event_type=vikunja_task_assigned',
        headers=actor_headers(admin['id']),
    )
    assert event_loaded.status_code == 200
    assert 'Nói thân mật hơn' in event_loaded.json()['content']


def test_admin_can_test_existing_reminder_rule_immediately(client, monkeypatch) -> None:
    calls = install_worker_success_stub(monkeypatch)
    users = client.get('/users').json()
    admin, _, _ = select_users(users)

    created = client.post(
        '/reminders',
        json={
            'name': 'UI test group digest',
            'rule_type': 'daily_group_digest',
            'target_channel': 'group',
            'target_id': 'test-zalo-group',
            'schedule_type': 'daily',
            'schedule_time': '08:00',
        },
        headers=actor_headers(admin['id']),
    )
    assert created.status_code == 201
    rule_id = created.json()['id']

    response = client.post(
        f'/admin/notifications/reminders/{rule_id}/test',
        headers=actor_headers(admin['id']),
    )

    assert response.status_code == 200
    data = response.json()
    assert data['rule_id'] == rule_id
    assert data['runs_created'] == 1
    assert data['targets_checked'] == 1
    assert data['targets'][0]['channel'] == 'group'
    assert data['dispatch']['sent'] == 1
    assert calls and calls[0]['channel'] == 'group'


def test_admin_can_bootstrap_core_daily_rules_and_read_scheduler_plan(client) -> None:
    users = client.get('/users').json()
    admin, member, _ = select_users(users)

    forbidden = client.post(
        '/admin/notifications/bootstrap-core-rules',
        headers=actor_headers(member['id']),
    )
    assert forbidden.status_code == 403

    bootstrapped = client.post(
        '/admin/notifications/bootstrap-core-rules',
        headers=actor_headers(admin['id']),
    )
    assert bootstrapped.status_code == 200
    data = bootstrapped.json()
    assert 'core_rule_summary' in data
    assert data['core_rule_summary']['daily_group_digest']['canonical_rule_id'] is not None
    assert data['core_rule_summary']['daily_member_checkin']['canonical_rule_id'] is not None
    assert data['core_rule_summary']['daily_strategy']['canonical_rule_id'] is not None

    plan = client.get(
        '/admin/notifications/scheduler/install-plan',
        headers=actor_headers(admin['id']),
    )
    assert plan.status_code == 200
    assert plan.json()['service_name'] == 'taskmanager-reminder-tick.service'


def test_admin_test_rule_reports_missing_target_cleanly(client) -> None:
    users = client.get('/users').json()
    admin, _, _ = select_users(users)

    created = client.post(
        '/reminders',
        json={
            'name': 'Broken task nudge',
            'rule_type': 'daily_group_digest',
            'target_channel': 'group',
            'schedule_type': 'daily',
            'schedule_time': '08:00',
            'enabled': True,
        },
        headers=actor_headers(admin['id']),
    )
    assert created.status_code == 201
    rule_id = created.json()['id']

    response = client.post(
        f'/admin/notifications/reminders/{rule_id}/test',
        headers=actor_headers(admin['id']),
    )

    assert response.status_code == 200
    data = response.json()
    assert data['targets_checked'] == 1
    assert data['runs_created'] == 1
    assert 'target' in (data['note'] or '').lower()


def test_morning_job_builds_group_admin_and_user_messages(client, db_session, monkeypatch) -> None:
    install_worker_success_stub(monkeypatch)
    settings = get_settings()

    users = client.get('/users').json()
    admin, member, _ = select_users(users)
    today = date.today()

    db_session.add_all(
        [
            Task(
                title='Today todo',
                status=TaskStatus.todo,
                assigned_to=member['id'],
                created_by=admin['id'],
                due_date=today,
                list_order=1,
            ),
            Task(
                title='Approved item',
                status=TaskStatus.ready,
                assigned_to=member['id'],
                created_by=admin['id'],
                list_order=2,
            ),
            Task(
                title='Pending admin review',
                status=TaskStatus.review,
                assigned_to=member['id'],
                created_by=member['id'],
                list_order=3,
            ),
        ]
    )
    db_session.commit()

    run = client.post(
        '/internal/notifications/run?job=morning',
        headers={'X-Internal-Token': settings.notify_internal_token or ''},
    )
    assert run.status_code == 200
    body = run.json()
    assert body['job'] == 'morning'
    assert body['events_created'] >= 3

    group_event = db_session.scalar(
        select(NotificationEvent).where(NotificationEvent.event_type == 'daily_morning_group')
    )
    admin_event = db_session.scalar(
        select(NotificationEvent).where(
            NotificationEvent.event_type == 'daily_morning_admin',
            NotificationEvent.user_id == admin['id'],
        )
    )
    user_event = db_session.scalar(
        select(NotificationEvent).where(
            NotificationEvent.event_type == 'daily_morning_user',
            NotificationEvent.user_id == member['id'],
        )
    )

    assert group_event is not None
    assert group_event.target_id == settings.zalo_group_id
    assert admin_event is not None
    assert user_event is not None
    assert '{{' not in user_event.payload.get('message', '')
    assert '{{' not in admin_event.payload.get('message', '')
    assert '{{' not in group_event.payload.get('message', '')


def test_evening_job_builds_group_summary(client, db_session, monkeypatch) -> None:
    install_worker_success_stub(monkeypatch)
    settings = get_settings()
    users = client.get('/users').json()
    admin, member, _ = select_users(users)

    db_session.add_all(
        [
            Task(
                title='Done now',
                status=TaskStatus.done,
                assigned_to=member['id'],
                created_by=admin['id'],
                list_order=1,
            ),
            Task(
                title='Still pending',
                status=TaskStatus.todo,
                assigned_to=member['id'],
                created_by=admin['id'],
                list_order=2,
            ),
        ]
    )
    db_session.commit()

    run = client.post(
        '/internal/notifications/run?job=evening',
        headers={'X-Internal-Token': settings.notify_internal_token or ''},
    )
    assert run.status_code == 200
    body = run.json()
    assert body['job'] == 'evening'
    assert body['events_created'] >= 1

    event = db_session.scalar(
        select(NotificationEvent).where(NotificationEvent.event_type == 'daily_evening_group')
    )
    assert event is not None
    assert event.target_id == settings.zalo_group_id
    assert '{{' not in event.payload.get('message', '')


def test_delivery_retry_then_failed(db_session, monkeypatch) -> None:
    def _always_fail(payload: dict):
        _ = payload
        return False, 500, '{"ok":false}', 'mock-fail'

    monkeypatch.setattr('app.notifications._call_worker', _always_fail)
    settings = get_settings()

    event, created = enqueue_notification_event(
        db_session,
        NotificationSpec(
            event_key='retry-test-001',
            event_type='retry_test',
            channel=NotificationChannel.user,
            target_id='zalo-user',
            payload={'message': 'Retry me', 'context': {'source': 'test'}},
        ),
    )
    assert created is True
    db_session.commit()

    max_attempts = max(1, settings.notification_max_retries + 1)
    for _ in range(max_attempts + 1):
        dispatch_due_notification_events(db_session, limit=10)
        db_session.refresh(event)
        if event.status == NotificationStatus.failed:
            break
        event.next_retry_at = None
        db_session.add(event)
        db_session.commit()

    assert event.status == NotificationStatus.failed
    assert event.attempt_count == max_attempts

    deliveries = db_session.scalars(
        select(NotificationDelivery).where(NotificationDelivery.event_id == event.id)
    ).all()
    assert len(deliveries) == max_attempts
