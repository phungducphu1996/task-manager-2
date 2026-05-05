"""add vikunja bridge mappings

Revision ID: 20260505_0012
Revises: 20260502_0011
Create Date: 2026-05-05 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings

revision: str = '20260505_0012'
down_revision: Union[str, Sequence[str], None] = '20260502_0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _already_exists(exc: Exception) -> bool:
    return 'already exists' in str(exc).lower()


def _not_exists(exc: Exception) -> bool:
    message = str(exc).lower()
    return 'does not exist' in message or 'undefined' in message


def _create_index_if_missing(inspector, name: str, table: str, columns: list[str], schema_name: str | None, *, unique: bool = False) -> None:
    indexes = inspector.get_indexes(table, schema=schema_name)
    if any(index.get('name') == name for index in indexes):
        return
    try:
        op.create_index(name, table, columns, unique=unique, schema=schema_name)
    except SQLAlchemyError as exc:
        if not _already_exists(exc):
            raise


def upgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table('vikunja_user_mappings', schema=schema_name):
        op.create_table(
            'vikunja_user_mappings',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('social_user_id', sa.String(length=64), nullable=False),
            sa.Column('vikunja_user_id', sa.Integer(), nullable=True),
            sa.Column('username', sa.String(length=120), nullable=False),
            sa.Column('display_name', sa.String(length=120), nullable=True),
            sa.Column('zalo_user_id', sa.String(length=64), nullable=True),
            sa.Column('role', sa.String(length=50), nullable=True),
            sa.Column('sync_status', sa.String(length=32), nullable=False),
            sa.Column('sync_error', sa.Text(), nullable=True),
            sa.Column('metadata', sa.JSON(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('social_user_id'),
            schema=schema_name,
        )

    if not inspector.has_table('vikunja_task_mappings', schema=schema_name):
        op.create_table(
            'vikunja_task_mappings',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('local_task_id', sa.Integer(), nullable=False),
            sa.Column('vikunja_task_id', sa.Integer(), nullable=True),
            sa.Column('vikunja_project_id', sa.Integer(), nullable=True),
            sa.Column('vikunja_bucket_id', sa.Integer(), nullable=True),
            sa.Column('source_status', sa.String(length=32), nullable=True),
            sa.Column('sync_status', sa.String(length=32), nullable=False),
            sa.Column('sync_error', sa.Text(), nullable=True),
            sa.Column('metadata', sa.JSON(), nullable=False),
            sa.Column('migrated_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['local_task_id'], ['tasks.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('local_task_id'),
            schema=schema_name,
        )

    if not inspector.has_table('vikunja_bridge_state', schema=schema_name):
        op.create_table(
            'vikunja_bridge_state',
            sa.Column('key', sa.String(length=120), nullable=False),
            sa.Column('value', sa.JSON(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('key'),
            schema=schema_name,
        )

    inspector = sa.inspect(bind)
    for table, indexes in {
        'vikunja_user_mappings': {
            op.f('ix_vikunja_user_mappings_id'): ['id'],
            op.f('ix_vikunja_user_mappings_social_user_id'): ['social_user_id'],
            op.f('ix_vikunja_user_mappings_vikunja_user_id'): ['vikunja_user_id'],
            op.f('ix_vikunja_user_mappings_username'): ['username'],
            op.f('ix_vikunja_user_mappings_zalo_user_id'): ['zalo_user_id'],
            op.f('ix_vikunja_user_mappings_sync_status'): ['sync_status'],
        },
        'vikunja_task_mappings': {
            op.f('ix_vikunja_task_mappings_id'): ['id'],
            op.f('ix_vikunja_task_mappings_local_task_id'): ['local_task_id'],
            op.f('ix_vikunja_task_mappings_vikunja_task_id'): ['vikunja_task_id'],
            op.f('ix_vikunja_task_mappings_vikunja_project_id'): ['vikunja_project_id'],
            op.f('ix_vikunja_task_mappings_vikunja_bucket_id'): ['vikunja_bucket_id'],
            op.f('ix_vikunja_task_mappings_source_status'): ['source_status'],
            op.f('ix_vikunja_task_mappings_sync_status'): ['sync_status'],
        },
    }.items():
        for name, columns in indexes.items():
            _create_index_if_missing(inspector, name, table, columns, schema_name)


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table in ('vikunja_bridge_state', 'vikunja_task_mappings', 'vikunja_user_mappings'):
        if not inspector.has_table(table, schema=schema_name):
            continue
        for index in inspector.get_indexes(table, schema=schema_name):
            name = index.get('name')
            if not name:
                continue
            try:
                op.drop_index(name, table_name=table, schema=schema_name)
            except SQLAlchemyError as exc:
                if not _not_exists(exc):
                    raise
        op.drop_table(table, schema=schema_name)
