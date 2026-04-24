"""add zalo incoming command idempotency table

Revision ID: 20260422_0008
Revises: 20260328_0007
Create Date: 2026-04-22 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '20260422_0008'
down_revision: Union[str, Sequence[str], None] = '20260328_0007'
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

    if not inspector.has_table('zalo_incoming_commands', schema=schema_name):
        op.create_table(
            'zalo_incoming_commands',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('message_key', sa.String(length=255), nullable=False),
            sa.Column('message_id', sa.String(length=128), nullable=True),
            sa.Column('conversation_id', sa.String(length=128), nullable=True),
            sa.Column('conversation_type', sa.String(length=32), nullable=True),
            sa.Column('from_uid', sa.String(length=64), nullable=True),
            sa.Column('text', sa.Text(), nullable=False),
            sa.Column('command', sa.String(length=32), nullable=False),
            sa.Column('task_id', sa.Integer(), nullable=True),
            sa.Column('response_payload', sa.JSON(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )

    inspector = sa.inspect(bind)

    def has_index(index_name: str) -> bool:
        indexes = inspector.get_indexes('zalo_incoming_commands', schema=schema_name)
        return any(index.get('name') == index_name for index in indexes)

    def create_index_if_missing(index_name: str, columns: list[str], *, unique: bool = False) -> None:
        if has_index(index_name):
            return
        try:
            op.create_index(index_name, 'zalo_incoming_commands', columns, unique=unique, schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_already_exists_error(exc):
                raise

    create_index_if_missing(op.f('ix_zalo_incoming_commands_id'), ['id'])
    create_index_if_missing(op.f('ix_zalo_incoming_commands_message_key'), ['message_key'], unique=True)
    create_index_if_missing(op.f('ix_zalo_incoming_commands_message_id'), ['message_id'])
    create_index_if_missing(op.f('ix_zalo_incoming_commands_conversation_id'), ['conversation_id'])
    create_index_if_missing(op.f('ix_zalo_incoming_commands_from_uid'), ['from_uid'])
    create_index_if_missing(op.f('ix_zalo_incoming_commands_task_id'), ['task_id'])


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table('zalo_incoming_commands', schema=schema_name):
        return

    def drop_index_if_exists(index_name: str) -> None:
        indexes = inspector.get_indexes('zalo_incoming_commands', schema=schema_name)
        if not any(index.get('name') == index_name for index in indexes):
            return
        try:
            op.drop_index(index_name, table_name='zalo_incoming_commands', schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_not_exists_error(exc):
                raise

    drop_index_if_exists(op.f('ix_zalo_incoming_commands_task_id'))
    drop_index_if_exists(op.f('ix_zalo_incoming_commands_from_uid'))
    drop_index_if_exists(op.f('ix_zalo_incoming_commands_conversation_id'))
    drop_index_if_exists(op.f('ix_zalo_incoming_commands_message_id'))
    drop_index_if_exists(op.f('ix_zalo_incoming_commands_message_key'))
    drop_index_if_exists(op.f('ix_zalo_incoming_commands_id'))
    try:
        op.drop_table('zalo_incoming_commands', schema=schema_name)
    except SQLAlchemyError as exc:
        if not _is_not_exists_error(exc):
            raise
