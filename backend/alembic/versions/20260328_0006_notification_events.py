"""add notification events and deliveries

Revision ID: 20260328_0006
Revises: 20260324_0005
Create Date: 2026-03-28 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '20260328_0006'
down_revision: Union[str, Sequence[str], None] = '20260324_0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_already_exists_error(exc: Exception) -> bool:
    return 'already exists' in str(exc).lower()


def _is_not_exists_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return 'does not exist' in message or 'undefined' in message


def upgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def has_table(table_name: str) -> bool:
        return inspector.has_table(table_name, schema=schema_name)

    def has_index(table_name: str, index_name: str) -> bool:
        indexes = inspector.get_indexes(table_name, schema=schema_name)
        return any(index.get('name') == index_name for index in indexes)

    def create_index_if_missing(index_name: str, table_name: str, columns: list[str], *, unique: bool = False) -> None:
        if has_index(table_name, index_name):
            return
        try:
            op.create_index(index_name, table_name, columns, unique=unique, schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_already_exists_error(exc):
                raise

    if not has_table('notification_events'):
        op.create_table(
            'notification_events',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('event_key', sa.String(length=255), nullable=False),
            sa.Column('event_type', sa.String(length=100), nullable=False),
            sa.Column(
                'channel',
                sa.Enum('user', 'group', name='notification_channel', native_enum=False),
                nullable=False,
            ),
            sa.Column('target_id', sa.String(length=128), nullable=True),
            sa.Column('task_id', sa.Integer(), nullable=True),
            sa.Column('user_id', sa.String(length=64), nullable=True),
            sa.Column('payload', sa.JSON(), nullable=False),
            sa.Column(
                'status',
                sa.Enum('pending', 'sent', 'failed', 'skipped', name='notification_status', native_enum=False),
                nullable=False,
                server_default='pending',
            ),
            sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )

    inspector = sa.inspect(bind)
    create_index_if_missing(op.f('ix_notification_events_id'), 'notification_events', ['id'])
    create_index_if_missing(op.f('ix_notification_events_event_key'), 'notification_events', ['event_key'], unique=True)
    create_index_if_missing(op.f('ix_notification_events_event_type'), 'notification_events', ['event_type'])
    create_index_if_missing(op.f('ix_notification_events_target_id'), 'notification_events', ['target_id'])
    create_index_if_missing(op.f('ix_notification_events_task_id'), 'notification_events', ['task_id'])
    create_index_if_missing(op.f('ix_notification_events_user_id'), 'notification_events', ['user_id'])
    create_index_if_missing(op.f('ix_notification_events_status'), 'notification_events', ['status'])
    create_index_if_missing(op.f('ix_notification_events_next_retry_at'), 'notification_events', ['next_retry_at'])

    if not has_table('notification_deliveries'):
        op.create_table(
            'notification_deliveries',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('event_id', sa.Integer(), nullable=False),
            sa.Column('attempt', sa.Integer(), nullable=False),
            sa.Column('request_payload', sa.JSON(), nullable=False),
            sa.Column('response_status', sa.Integer(), nullable=True),
            sa.Column('response_body', sa.Text(), nullable=True),
            sa.Column('error', sa.Text(), nullable=True),
            sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['event_id'], ['notification_events.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )

    inspector = sa.inspect(bind)
    create_index_if_missing(op.f('ix_notification_deliveries_id'), 'notification_deliveries', ['id'])
    create_index_if_missing(op.f('ix_notification_deliveries_event_id'), 'notification_deliveries', ['event_id'])


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def drop_index_if_exists(index_name: str, table_name: str) -> None:
        indexes = inspector.get_indexes(table_name, schema=schema_name)
        if not any(index.get('name') == index_name for index in indexes):
            return
        try:
            op.drop_index(index_name, table_name=table_name, schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_not_exists_error(exc):
                raise

    if inspector.has_table('notification_deliveries', schema=schema_name):
        drop_index_if_exists(op.f('ix_notification_deliveries_event_id'), 'notification_deliveries')
        drop_index_if_exists(op.f('ix_notification_deliveries_id'), 'notification_deliveries')
        try:
            op.drop_table('notification_deliveries', schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_not_exists_error(exc):
                raise

    inspector = sa.inspect(bind)
    if inspector.has_table('notification_events', schema=schema_name):
        drop_index_if_exists(op.f('ix_notification_events_next_retry_at'), 'notification_events')
        drop_index_if_exists(op.f('ix_notification_events_status'), 'notification_events')
        drop_index_if_exists(op.f('ix_notification_events_user_id'), 'notification_events')
        drop_index_if_exists(op.f('ix_notification_events_task_id'), 'notification_events')
        drop_index_if_exists(op.f('ix_notification_events_target_id'), 'notification_events')
        drop_index_if_exists(op.f('ix_notification_events_event_type'), 'notification_events')
        drop_index_if_exists(op.f('ix_notification_events_event_key'), 'notification_events')
        drop_index_if_exists(op.f('ix_notification_events_id'), 'notification_events')
        try:
            op.drop_table('notification_events', schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_not_exists_error(exc):
                raise
