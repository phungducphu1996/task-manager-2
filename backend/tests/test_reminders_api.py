from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import get_settings
from app.models import (
    NotificationChannel,
    ReminderInteraction,
    ReminderInteractionType,
    ReminderRule,
    ReminderRuleType,
    ReminderRun,
    ReminderRunStatus,
    ReminderScheduleType,
    Task,
    TaskStatus,
)
from app.reminders import run_reminder_tick


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


def test_internal_reminder_tick_requires_token(client) -> None:
    missing = client.post('/internal/reminders/tick')
    assert missing.status_code == 401

    wrong = client.post('/internal/reminders/tick', headers={'X-Internal-Token': 'wrong-token'})
    assert wrong.status_code == 403


def test_daily_group_digest_tick_creates_one_run_and_dedupes(client, db_session, monkeypatch) -> None:
    calls = install_worker_success_stub(monkeypatch)
    settings = get_settings()
    admin, _, _ = select_users(client.get('/users').json())

    created = client.post(
        '/reminders',
        json={
            'name': 'Daily group digest',
            'rule_type': 'daily_group_digest',
            'target_channel': 'group',
            'target_id': settings.zalo_group_id,
            'schedule_type': 'daily',
            'schedule_time': '00:00:00',
        },
        headers=actor_headers(admin['id']),
    )
    assert created.status_code == 201

    first = client.post('/internal/reminders/tick', headers={'X-Internal-Token': settings.notify_internal_token or ''})
    second = client.post('/internal/reminders/tick', headers={'X-Internal-Token': settings.notify_internal_token or ''})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()['runs_created'] == 1
    assert second.json()['runs_created'] == 0
    assert second.json()['runs_deduped'] >= 1

    runs = db_session.scalars(select(ReminderRun)).all()
    assert len(runs) == 1
    assert calls and calls[0]['channel'] == 'group'


def test_task_nudge_stops_when_task_is_review(client, db_session, monkeypatch) -> None:
    calls = install_worker_success_stub(monkeypatch)
    settings = get_settings()
    admin, member, _ = select_users(client.get('/users').json())
    task = Task(
        title='Review already',
        status=TaskStatus.review,
        assigned_to=member['id'],
        created_by=admin['id'],
        list_order=1,
    )
    db_session.add(task)
    db_session.commit()

    created = client.post(
        f'/tasks/{task.id}/reminders?interval_minutes=60',
        headers=actor_headers(admin['id']),
    )
    assert created.status_code == 201

    tick = client.post('/internal/reminders/tick', headers={'X-Internal-Token': settings.notify_internal_token or ''})
    assert tick.status_code == 200
    assert tick.json()['runs_created'] == 0
    assert db_session.scalars(select(ReminderRun)).all() == []
    assert calls == []


def test_zalo_ack_marks_latest_reminder_run(client, db_session, monkeypatch) -> None:
    install_worker_success_stub(monkeypatch)
    settings = get_settings()
    admin, member, _ = select_users(client.get('/users').json())
    task = Task(
        title='Ack me',
        status=TaskStatus.todo,
        assigned_to=member['id'],
        created_by=admin['id'],
        list_order=1,
    )
    db_session.add(task)
    db_session.commit()
    rule = ReminderRule(
        name='Ack rule',
        rule_type=ReminderRuleType.task_nudge,
        enabled=True,
        target_channel=NotificationChannel.user,
        target_id=member['zalo_user_id'],
        user_id=member['id'],
        task_id=task.id,
        schedule_type=ReminderScheduleType.interval,
        interval_minutes=60,
        timezone=settings.reminder_timezone,
        quiet_start=time(22, 0),
        quiet_end=time(7, 0),
        max_runs_per_day=6,
        stop_statuses=[TaskStatus.review.value, TaskStatus.ready.value, TaskStatus.done.value],
        escalation_after_runs=3,
        payload={},
        created_by=admin['id'],
    )
    db_session.add(rule)
    db_session.commit()

    tick = client.post('/internal/reminders/tick', headers={'X-Internal-Token': settings.notify_internal_token or ''})
    assert tick.status_code == 200
    run = db_session.scalar(select(ReminderRun).where(ReminderRun.rule_id == rule.id))
    assert run is not None

    response = client.post(
        '/zalo/incoming',
        json={
            'text': 'ok',
            'from_uid': member['zalo_user_id'],
            'conversation_id': member['zalo_user_id'],
            'conversation_type': 'user',
            'message_id': 'ack-message-1',
        },
        headers={'X-Internal-Secret': settings.zalo_shared_secret or ''},
    )
    assert response.status_code == 200
    assert response.json()['action'] == 'reminder_ack'
    db_session.refresh(run)
    assert run.status == ReminderRunStatus.acknowledged
    interaction = db_session.scalar(select(ReminderInteraction).where(ReminderInteraction.run_id == run.id))
    assert interaction is not None
    assert interaction.interaction_type == ReminderInteractionType.ack


def test_member_checkin_escalates_to_admin_after_delay(client, db_session, monkeypatch) -> None:
    install_worker_success_stub(monkeypatch)
    settings = get_settings()
    admin, _, _ = select_users(client.get('/users').json())
    rule = ReminderRule(
        name='Member checkin',
        rule_type=ReminderRuleType.daily_member_checkin,
        enabled=True,
        target_channel=None,
        target_id=None,
        user_id=None,
        task_id=None,
        schedule_type=ReminderScheduleType.daily,
        schedule_time=time(9, 0),
        interval_minutes=None,
        timezone=settings.reminder_timezone,
        quiet_start=time(22, 0),
        quiet_end=time(7, 0),
        max_runs_per_day=None,
        stop_statuses=[],
        escalation_after_minutes=60,
        escalation_after_runs=None,
        payload={},
        created_by=admin['id'],
    )
    db_session.add(rule)
    db_session.commit()

    tz = ZoneInfo(settings.reminder_timezone)
    run_reminder_tick(db_session, now=datetime(2026, 5, 2, 9, 0, tzinfo=tz))
    escalated = run_reminder_tick(db_session, now=datetime(2026, 5, 2, 10, 1, tzinfo=tz))

    assert escalated['escalations_created'] >= 1
