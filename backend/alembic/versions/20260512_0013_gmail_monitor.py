"""add gmail monitor events

Revision ID: 20260512_0013
Revises: 20260505_0012
Create Date: 2026-05-12 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings

revision: str = '20260512_0013'
down_revision: Union[str, Sequence[str], None] = '20260505_0012'
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

    if not inspector.has_table('gmail_monitor_events', schema=schema_name):
        op.create_table(
            'gmail_monitor_events',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('gmail_message_id', sa.String(length=255), nullable=False),
            sa.Column('gmail_thread_id', sa.String(length=255), nullable=True),
            sa.Column('rfc_message_id', sa.String(length=255), nullable=True),
            sa.Column('event_type', sa.String(length=32), nullable=False),
            sa.Column('source', sa.String(length=64), nullable=False),
            sa.Column('sender', sa.String(length=255), nullable=True),
            sa.Column('recipient', sa.String(length=255), nullable=True),
            sa.Column('subject', sa.String(length=500), nullable=False),
            sa.Column('snippet', sa.Text(), nullable=True),
            sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('sale_order_id', sa.String(length=64), nullable=True),
            sa.Column('sale_total_cents', sa.Integer(), nullable=True),
            sa.Column('sale_currency', sa.String(length=12), nullable=True),
            sa.Column('buyer_name', sa.String(length=255), nullable=True),
            sa.Column('buyer_username', sa.String(length=255), nullable=True),
            sa.Column('order_url', sa.String(length=1000), nullable=True),
            sa.Column('notification_event_id', sa.Integer(), nullable=True),
            sa.Column('payload', sa.JSON(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['notification_event_id'], ['notification_events.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('gmail_message_id'),
            schema=schema_name,
        )

    if not inspector.has_table('integration_configs', schema=schema_name):
        op.create_table(
            'integration_configs',
            sa.Column('key', sa.String(length=120), nullable=False),
            sa.Column('payload', sa.JSON(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('key'),
            schema=schema_name,
        )

    inspector = sa.inspect(bind)
    for name, columns in {
        op.f('ix_gmail_monitor_events_id'): ['id'],
        op.f('ix_gmail_monitor_events_gmail_message_id'): ['gmail_message_id'],
        op.f('ix_gmail_monitor_events_gmail_thread_id'): ['gmail_thread_id'],
        op.f('ix_gmail_monitor_events_rfc_message_id'): ['rfc_message_id'],
        op.f('ix_gmail_monitor_events_event_type'): ['event_type'],
        op.f('ix_gmail_monitor_events_received_at'): ['received_at'],
        op.f('ix_gmail_monitor_events_sale_order_id'): ['sale_order_id'],
        op.f('ix_gmail_monitor_events_notification_event_id'): ['notification_event_id'],
    }.items():
        _create_index_if_missing(inspector, name, 'gmail_monitor_events', columns, schema_name)


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table('integration_configs', schema=schema_name):
        op.drop_table('integration_configs', schema=schema_name)

    if inspector.has_table('gmail_monitor_events', schema=schema_name):
        for index in inspector.get_indexes('gmail_monitor_events', schema=schema_name):
            name = index.get('name')
            if not name:
                continue
            try:
                op.drop_index(name, table_name='gmail_monitor_events', schema=schema_name)
            except SQLAlchemyError as exc:
                if not _not_exists(exc):
                    raise
        op.drop_table('gmail_monitor_events', schema=schema_name)
