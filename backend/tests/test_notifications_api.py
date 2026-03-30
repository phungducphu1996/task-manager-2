from __future__ import annotations

from datetime import date

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


def test_internal_notification_job_requires_token(client) -> None:
    missing = client.post('/internal/notifications/run?job=morning')
    assert missing.status_code == 401

    wrong = client.post('/internal/notifications/run?job=morning', headers={'X-Internal-Token': 'wrong-token'})
    assert wrong.status_code == 403


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
