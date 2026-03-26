from collections.abc import Generator

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()
default_metadata = (
    MetaData(schema=settings.normalized_db_schema) if settings.normalized_db_schema else MetaData()
)


class Base(DeclarativeBase):
    metadata = default_metadata


def _uses_supabase_pooler(url: str) -> bool:
    lowered = url.lower()
    return 'pooler.supabase.com' in lowered or ':6543' in lowered


engine_kwargs: dict[str, object] = {'future': True}
if settings.database_url.startswith('sqlite'):
    engine_kwargs['connect_args'] = {'check_same_thread': False}
elif _uses_supabase_pooler(settings.database_url):
    # Supabase pooler (PgBouncer transaction mode) is incompatible with
    # psycopg prepared statements unless they are disabled.
    engine_kwargs['connect_args'] = {'prepare_threshold': None}

engine = create_engine(settings.database_url, **engine_kwargs)

schema_name = settings.normalized_db_schema
if schema_name and not settings.database_url.startswith('sqlite'):
    # Keep all app queries scoped to a dedicated schema in shared Postgres databases.
    @event.listens_for(engine, 'connect')
    def set_search_path(dbapi_connection, connection_record) -> None:  # type: ignore[no-redef]
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')
            cursor.execute(f'SET search_path TO "{schema_name}"')
        finally:
            cursor.close()

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
