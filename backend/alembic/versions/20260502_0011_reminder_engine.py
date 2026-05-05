"""add reminder engine

Revision ID: 20260502_0011
Revises: 20260427_0010
Create Date: 2026-05-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '20260502_0011'
down_revision: Union[str, Sequence[str], None] = '20260427_0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _already_exists(exc: Exception) -> bool:
    return 'already exists' in str(exc).lower()


def _not_exists(exc: Exception) -> bool:
    message = str(exc).lower()
    return 'does not exist' in message or 'undefined' in message


def _create_index_if_missing(inspector, name: str, table: str, columns: list[str], schema_name: str | None) -> None:
    indexes = inspector.get_indexes(table, schema=schema_name)
    if any(index.get('name') == name for index in indexes):
        return
    try:
        op.create_index(name, table, columns, unique=False, schema=schema_name)
    except SQLAlchemyError as exc:
        if not _already_exists(exc):
            raise


def upgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table('reminder_rules', schema=schema_name):
        op.create_table(
            'reminder_rules',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=255), nullable=False),
            sa.Column('rule_type', sa.String(length=32), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False),
            sa.Column('target_channel', sa.String(length=16), nullable=True),
            sa.Column('target_id', sa.String(length=128), nullable=True),
            sa.Column('user_id', sa.String(length=64), nullable=True),
            sa.Column('task_id', sa.Integer(), nullable=True),
            sa.Column('schedule_type', sa.String(length=32), nullable=False),
            sa.Column('schedule_time', sa.Time(), nullable=True),
            sa.Column('interval_minutes', sa.Integer(), nullable=True),
            sa.Column('timezone', sa.String(length=64), nullable=False),
            sa.Column('quiet_start', sa.Time(), nullable=True),
            sa.Column('quiet_end', sa.Time(), nullable=True),
            sa.Column('max_runs_per_day', sa.Integer(), nullable=True),
            sa.Column('stop_statuses', sa.JSON(), nullable=False),
            sa.Column('escalation_after_minutes', sa.Integer(), nullable=True),
            sa.Column('escalation_after_runs', sa.Integer(), nullable=True),
            sa.Column('payload', sa.JSON(), nullable=False),
            sa.Column('created_by', sa.String(length=64), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )

    if not inspector.has_table('reminder_runs', schema=schema_name):
        op.create_table(
            'reminder_runs',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('rule_id', sa.Integer(), nullable=False),
            sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=False),
            sa.Column('status', sa.String(length=32), nullable=False),
            sa.Column('notification_event_id', sa.Integer(), nullable=True),
            sa.Column('run_key', sa.String(length=255), nullable=False),
            sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('snoozed_until', sa.DateTime(timezone=True), nullable=True),
            sa.Column('escalated_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['notification_event_id'], ['notification_events.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['rule_id'], ['reminder_rules.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('run_key'),
            schema=schema_name,
        )

    if not inspector.has_table('reminder_interactions', schema=schema_name):
        op.create_table(
            'reminder_interactions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('run_id', sa.Integer(), nullable=True),
            sa.Column('rule_id', sa.Integer(), nullable=True),
            sa.Column('user_id', sa.String(length=64), nullable=True),
            sa.Column('conversation_id', sa.String(length=128), nullable=True),
            sa.Column('message_id', sa.String(length=128), nullable=True),
            sa.Column('interaction_type', sa.String(length=32), nullable=False),
            sa.Column('text', sa.Text(), nullable=True),
            sa.Column('payload', sa.JSON(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['rule_id'], ['reminder_rules.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['run_id'], ['reminder_runs.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )

    inspector = sa.inspect(bind)
    for table, indexes in {
        'reminder_rules': {
            op.f('ix_reminder_rules_id'): ['id'],
            op.f('ix_reminder_rules_rule_type'): ['rule_type'],
            op.f('ix_reminder_rules_enabled'): ['enabled'],
            op.f('ix_reminder_rules_target_id'): ['target_id'],
            op.f('ix_reminder_rules_user_id'): ['user_id'],
            op.f('ix_reminder_rules_task_id'): ['task_id'],
            op.f('ix_reminder_rules_created_by'): ['created_by'],
        },
        'reminder_runs': {
            op.f('ix_reminder_runs_id'): ['id'],
            op.f('ix_reminder_runs_rule_id'): ['rule_id'],
            op.f('ix_reminder_runs_scheduled_for'): ['scheduled_for'],
            op.f('ix_reminder_runs_status'): ['status'],
            op.f('ix_reminder_runs_notification_event_id'): ['notification_event_id'],
            op.f('ix_reminder_runs_run_key'): ['run_key'],
            op.f('ix_reminder_runs_snoozed_until'): ['snoozed_until'],
        },
        'reminder_interactions': {
            op.f('ix_reminder_interactions_id'): ['id'],
            op.f('ix_reminder_interactions_run_id'): ['run_id'],
            op.f('ix_reminder_interactions_rule_id'): ['rule_id'],
            op.f('ix_reminder_interactions_user_id'): ['user_id'],
            op.f('ix_reminder_interactions_conversation_id'): ['conversation_id'],
            op.f('ix_reminder_interactions_message_id'): ['message_id'],
            op.f('ix_reminder_interactions_interaction_type'): ['interaction_type'],
        },
    }.items():
        for name, columns in indexes.items():
            _create_index_if_missing(inspector, name, table, columns, schema_name)


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table in ('reminder_interactions', 'reminder_runs', 'reminder_rules'):
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
