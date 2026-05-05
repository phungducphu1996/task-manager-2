from __future__ import annotations

from app.config import get_settings
from app.models import Task, TaskStatus, VikunjaTaskMapping, VikunjaUserMapping
import app.main as main_module


def internal_headers() -> dict[str, str]:
    return {'X-Internal-Token': get_settings().notify_internal_token or ''}


class DummyVikunjaClient:
    def __init__(self) -> None:
        self.created_projects: list[dict] = []
        self.created_tasks: list[dict] = []
        self.comments: list[tuple[int, str]] = []

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


def configure_vikunja(monkeypatch, client: DummyVikunjaClient):
    settings = get_settings()
    monkeypatch.setattr(settings, 'vikunja_api_url', 'http://vikunja.local')
    monkeypatch.setattr(settings, 'vikunja_api_token', 'test-token')
    monkeypatch.setattr(settings, 'vikunja_project_id', None)
    monkeypatch.setattr(main_module, 'get_vikunja_client', lambda: client)


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
