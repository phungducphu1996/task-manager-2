import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, close_all_sessions, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ['DATABASE_URL'] = 'sqlite:///./test_task_manager.db'
os.environ['APP_TIMEZONE'] = 'Asia/Ho_Chi_Minh'
os.environ['DB_SCHEMA'] = ''
os.environ['SUPABASE_URL'] = ''
os.environ['SUPABASE_SERVICE_ROLE_KEY'] = ''
os.environ['SUPABASE_STORAGE_BUCKET'] = ''
os.environ['NOTIFY_INTERNAL_TOKEN'] = 'test-internal-token'
os.environ['ZALO_WORKER_URL'] = 'http://worker.local/notify'
os.environ['ZALO_WORKER_TOKEN'] = 'test-zalo-worker-token'
os.environ['ZALO_SHARED_SECRET'] = 'test-zalo-secret'
os.environ['ZALO_GROUP_ID'] = 'test-zalo-group'
os.environ['ZALO_ALLOWED_GROUP_IDS'] = 'test-zalo-group'
os.environ['ZALO_BOT_ALIASES'] = '@TaskBot,@task'
os.environ['TASK_PUBLIC_BASE_URL'] = 'https://hazeleo.com/task'

from app.database import Base, get_db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
import app.database as database_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.models import User  # noqa: E402
from app.services import seed_reference_data  # noqa: E402

test_engine = create_engine(
    'sqlite://',
    connect_args={'check_same_thread': False},
    poolclass=StaticPool,
    future=True,
)
TestingSessionLocal = sessionmaker(bind=test_engine, autocommit=False, autoflush=False, class_=Session)
database_module.engine = test_engine
database_module.SessionLocal = TestingSessionLocal
main_module.engine = test_engine


@pytest.fixture(autouse=True)
def isolated_bot_paths(tmp_path) -> None:
    settings = get_settings()
    settings.bot_persona_path = str(tmp_path / 'bot' / 'persona' / 'core.md')
    settings.bot_notification_prompt_path = str(tmp_path / 'bot' / 'persona' / 'notifications.md')
    settings.bot_profiles_dir = str(tmp_path / 'bot' / 'profiles')
    settings.bot_contacts_path = str(tmp_path / 'bot' / 'contacts.md')
    settings.bot_contact_prompts_dir = str(tmp_path / 'bot' / 'contact-prompts')
    settings.bot_events_path = str(tmp_path / 'bot' / 'events.md')
    settings.openai_api_key = None


@pytest.fixture(autouse=True)
def clean_database() -> None:
    close_all_sessions()
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    with Session(test_engine) as db:
        seed_reference_data(db)
        db.add_all(
            [
                User(
                    id=str(uuid4()),
                    username='linh',
                    full_name='Linh',
                    zalo_user_id='zalo-linh',
                    password_hash='linh123',
                    role='designer',
                    is_active=True,
                ),
                User(
                    id=str(uuid4()),
                    username='quang',
                    full_name='Quang',
                    zalo_user_id='zalo-quang',
                    password_hash='quang123',
                    role='content',
                    is_active=True,
                ),
                User(
                    id=str(uuid4()),
                    username='trang',
                    full_name='Trang',
                    zalo_user_id='zalo-trang',
                    password_hash='trang123',
                    role='admin',
                    is_active=True,
                ),
            ]
        )
        db.commit()


@pytest.fixture()
def db_session() -> Session:
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client() -> TestClient:
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
