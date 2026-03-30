import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ['DATABASE_URL'] = 'sqlite:///./test_task_manager.db'
os.environ['APP_TIMEZONE'] = 'Asia/Ho_Chi_Minh'
os.environ['DB_SCHEMA'] = ''
os.environ['SUPABASE_URL'] = ''
os.environ['SUPABASE_SERVICE_ROLE_KEY'] = ''
os.environ['SUPABASE_STORAGE_BUCKET'] = ''
os.environ['NOTIFY_INTERNAL_TOKEN'] = 'test-internal-token'
os.environ['ZALO_WORKER_URL'] = 'http://worker.local/notify'
os.environ['ZALO_WORKER_TOKEN'] = 'test-zalo-worker-token'
os.environ['ZALO_GROUP_ID'] = 'test-zalo-group'

from app.database import Base, SessionLocal, engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.services import seed_reference_data  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
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
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client() -> TestClient:
    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
