from __future__ import annotations

from app.config import get_settings
from app.models import NotificationEvent, Task, TaskStatus, VikunjaBridgeState, VikunjaTaskMapping, VikunjaUserMapping
import app.main as main_module
import app.vikunja as vikunja_module


def internal_headers() -> dict[str, str]:
    return {'X-Internal-Token': get_settings().notify_internal_token or ''}


class DummyVikunjaClient:
    def __init__(self) -> None:
        self.created_projects: list[dict] = []
        self.created_tasks: list[dict] = []
        self.comments: list[tuple[int, str]] = []
        self.project_tasks: list[dict] = []
        self.project_views: list[dict] = []
        self.view_tasks_by_filter: dict[str | None, list[dict]] = {}

    def find_users(self, search: str) -> list[dict]:
        return [{'id': abs(hash(search)) % 10000 + 1, 'username': search}]

    def create_project(self, title: str) -> dict:
        self.created_projects.append({'title': title})
        return {'id': 777, 'title': title}

    def create_task(self, project_id: int, payload: dict) -> dict:
        task_id = len(self.created_tasks) + 100
        self.created_tasks.append({'project_id': project_id, 'payload': payload})
        return {'id': task_id, **payload}

    def create_task_comment(self, task_id: int, comment: str) -> dict:
        self.comments.append((task_id, comment))
        return {'id': len(self.comments), 'comment': comment}

    def list_all_project_tasks(self, project_id: int) -> list[dict]:
        return self.project_tasks

    def list_project_views(self, project_id: int) -> list[dict]:
        return self.project_views

    def list_all_view_tasks(self, project_id: int, view_id: int, *, filter_query: str | None = None, **kwargs) -> list[dict]:
        return self.view_tasks_by_filter.get(filter_query, [])


def configure_vikunja(monkeypatch, client: DummyVikunjaClient):
    settings = get_settings()
    monkeypatch.setattr(settings, 'vikunja_api_url', 'http://vikunja.local')
    monkeypatch.setattr(settings, 'vikunja_api_token', 'test-token')
    monkeypatch.setattr(settings, 'vikunja_project_id', None)
    monkeypatch.setattr(main_module, 'get_vikunja_client', lambda: client)
    monkeypatch.setattr(vikunja_module, 'get_vikunja_client', lambda: client)


def test_vikunja_status_requires_internal_token(client) -> None:
    response = client.get('/internal/vikunja/status')
    assert response.status_code == 401

    allowed = client.get('/internal/vikunja/status', headers=internal_headers())
    assert allowed.status_code == 200
    assert allowed.json()['configured'] is False


def test_vikunja_sync_users_creates_mappings(client, db_session, monkeypatch) -> None:
    dummy = DummyVikunjaClient()
    configure_vikunja(monkeypatch, dummy)

    response = client.post('/internal/vikunja/sync-users', headers=internal_headers())
    assert response.status_code == 200
    data = response.json()
    assert data['matched'] == 3
    assert db_session.query(VikunjaUserMapping).count() == 3


def test_vikunja_migrate_tasks_dry_run_and_live(client, db_session, monkeypatch) -> None:
    dummy = DummyVikunjaClient()
    configure_vikunja(monkeypatch, dummy)
    users = client.get('/users').json()
    admin = next(user for user in users if user['role'] == 'admin')
    task = Task(
        title='Migrate me',
        status=TaskStatus.todo,
        assigned_to=admin['id'],
        created_by=admin['id'],
        list_order=1,
    )
    db_session.add(task)
    db_session.commit()

    dry_run = client.post('/internal/vikunja/migrate-tasks?dry_run=true', headers=internal_headers())
    assert dry_run.status_code == 200
    assert dry_run.json()['migrated'] == 0
    assert dry_run.json()['skipped'] >= 1
    assert db_session.query(VikunjaTaskMapping).count() == 0

    live = client.post('/internal/vikunja/migrate-tasks', headers=internal_headers())
    assert live.status_code == 200
    assert live.json()['migrated'] >= 1
    assert dummy.created_tasks
    assert db_session.query(VikunjaTaskMapping).count() >= 1


def test_vikunja_webhook_records_payload(client, db_session, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, 'vikunja_webhook_secret', 'secret')

    rejected = client.post('/vikunja/webhook', json={'event': 'task.updated'})
    assert rejected.status_code == 401

    accepted = client.post('/vikunja/webhook', json={'event': 'task.updated'}, headers={'X-Vikunja-Secret': 'secret'})
    assert accepted.status_code == 200
    assert accepted.json()['ok'] is True


def test_vikunja_reconcile_seeds_then_notifies_changes(client, db_session, monkeypatch) -> None:
    dummy = DummyVikunjaClient()
    configure_vikunja(monkeypatch, dummy)
    settings = get_settings()
    monkeypatch.setattr(settings, 'vikunja_project_id', 9)

    sync = client.post('/internal/vikunja/sync-users', headers=internal_headers())
    assert sync.status_code == 200
    admin_mapping = db_session.query(VikunjaUserMapping).first()
    assert admin_mapping is not None
    dummy.project_tasks = [
        {
            'id': 501,
            'title': 'Initial Vikunja task',
            'done': False,
            'due_date': None,
            'updated': '2026-05-06T09:00:00+07:00',
            'assignees': [{'id': admin_mapping.vikunja_user_id, 'username': 'admin'}],
        }
    ]

    first = client.post('/internal/vikunja/reconcile', headers=internal_headers())
    assert first.status_code == 200
    assert first.json()['reconcile']['seeded'] == 1
    assert db_session.query(NotificationEvent).count() == 0

    dummy.project_tasks[0] = {**dummy.project_tasks[0], 'title': 'Updated Vikunja task', 'updated': '2026-05-06T09:05:00+07:00'}
    second = client.post('/internal/vikunja/reconcile', headers=internal_headers())
    assert second.status_code == 200
    assert second.json()['reconcile']['changed'] == 1
    event = db_session.query(NotificationEvent).filter(NotificationEvent.event_type == 'vikunja_task_updated').one()
    assert event.payload['context']['vikunja_task_id'] == 501


def test_vikunja_reconcile_uses_kanban_bucket_for_status(client, db_session, monkeypatch) -> None:
    dummy = DummyVikunjaClient()
    configure_vikunja(monkeypatch, dummy)
    settings = get_settings()
    monkeypatch.setattr(settings, 'vikunja_project_id', 9)
    monkeypatch.setattr(settings, 'vikunja_bucket_doing_id', 26)
    dummy.project_views = [{'id': 36, 'view_kind': 'kanban'}]
    dummy.project_tasks = [
        {
            'id': 601,
            'title': 'Doing without bucket id in task payload',
            'done': False,
            'due_date': None,
            'updated': '2026-05-06T09:00:00+07:00',
            'assignees': [],
        }
    ]
    dummy.view_tasks_by_filter = {
        'bucket_id = 26': [{'id': 601, 'bucket_id': 26, 'done': False}],
    }

    response = client.post('/internal/vikunja/reconcile', headers=internal_headers())

    assert response.status_code == 200
    state = db_session.get(VikunjaBridgeState, 'vikunja_task:601')
    assert state is not None
    assert state.value['status'] == 'doing'
