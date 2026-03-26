from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy import engine_from_config, pool, text

from app.config import get_settings
from app.database import Base
from app.models import Shop, Subtask, Task, TaskType, User  # noqa: F401

config = context.config
settings = get_settings()
# Alembic uses configparser interpolation; percent signs in URL-encoded passwords
# (for example `%40`) must be escaped as `%%` before setting sqlalchemy.url.
config.set_main_option('sqlalchemy.url', settings.database_url.replace('%', '%%'))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
version_table_name = 'alembic_version_teamtask_manager'


def _uses_supabase_pooler(url: str) -> bool:
    lowered = url.lower()
    return 'pooler.supabase.com' in lowered or ':6543' in lowered


def _version_table_ref(schema_name: str | None) -> str:
    if schema_name:
        return f'"{schema_name}"."{version_table_name}"'
    return f'"{version_table_name}"'


def _repair_unknown_revision_rows(connection, schema_name: str | None) -> None:
    inspector = sa.inspect(connection)
    if not inspector.has_table(version_table_name, schema=schema_name):
        return

    script = ScriptDirectory.from_config(config)
    version_table = _version_table_ref(schema_name)
    versions = connection.execute(text(f'SELECT version_num FROM {version_table}')).scalars().all()
    unknown_versions = [version for version in versions if script.get_revision(version) is None]
    for version in unknown_versions:
        connection.execute(text(f'DELETE FROM {version_table} WHERE version_num = :version_num'), {'version_num': version})


def run_migrations_offline() -> None:
    url = config.get_main_option('sqlalchemy.url')
    schema_name = settings.normalized_db_schema
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
        compare_type=True,
        version_table=version_table_name,
        version_table_schema=schema_name,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connect_args: dict[str, object] = {}
    if not settings.database_url.startswith('sqlite') and _uses_supabase_pooler(settings.database_url):
        # Supabase pooler (PgBouncer transaction mode) doesn't work well with
        # psycopg prepared statements during migrations.
        connect_args['prepare_threshold'] = None

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    with connectable.connect() as connection:
        schema_name = settings.normalized_db_schema
        if schema_name:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))
            connection.execute(text(f'SET search_path TO "{schema_name}"'))

        _repair_unknown_revision_rows(connection, schema_name)

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            version_table=version_table_name,
            version_table_schema=schema_name,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
