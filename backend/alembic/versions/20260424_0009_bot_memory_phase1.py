"""add bot memory phase 1 tables

Revision ID: 20260424_0009
Revises: 20260422_0008
Create Date: 2026-04-24 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '20260424_0009'
down_revision: Union[str, Sequence[str], None] = '20260422_0008'
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

    if not inspector.has_table('bot_conversation_messages', schema=schema_name):
        op.create_table(
            'bot_conversation_messages',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.String(length=64), nullable=True),
            sa.Column('conversation_id', sa.String(length=128), nullable=True),
            sa.Column('message_id', sa.String(length=128), nullable=True),
            sa.Column('role', sa.String(length=16), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('metadata', sa.JSON(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )

    if not inspector.has_table('bot_memory_facts', schema=schema_name):
        op.create_table(
            'bot_memory_facts',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.String(length=64), nullable=True),
            sa.Column('category', sa.String(length=32), nullable=False),
            sa.Column('fact', sa.Text(), nullable=False),
            sa.Column('confidence', sa.Integer(), nullable=False, server_default='60'),
            sa.Column('source_message_id', sa.String(length=128), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )

    inspector = sa.inspect(bind)

    def has_index(table_name: str, index_name: str) -> bool:
        indexes = inspector.get_indexes(table_name, schema=schema_name)
        return any(index.get('name') == index_name for index in indexes)

    def create_index_if_missing(table_name: str, index_name: str, columns: list[str]) -> None:
        if has_index(table_name, index_name):
            return
        try:
            op.create_index(index_name, table_name, columns, unique=False, schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_already_exists_error(exc):
                raise

    create_index_if_missing('bot_conversation_messages', op.f('ix_bot_conversation_messages_id'), ['id'])
    create_index_if_missing('bot_conversation_messages', op.f('ix_bot_conversation_messages_user_id'), ['user_id'])
    create_index_if_missing(
        'bot_conversation_messages',
        op.f('ix_bot_conversation_messages_conversation_id'),
        ['conversation_id'],
    )
    create_index_if_missing('bot_conversation_messages', op.f('ix_bot_conversation_messages_message_id'), ['message_id'])
    create_index_if_missing('bot_conversation_messages', op.f('ix_bot_conversation_messages_role'), ['role'])

    create_index_if_missing('bot_memory_facts', op.f('ix_bot_memory_facts_id'), ['id'])
    create_index_if_missing('bot_memory_facts', op.f('ix_bot_memory_facts_user_id'), ['user_id'])
    create_index_if_missing('bot_memory_facts', op.f('ix_bot_memory_facts_category'), ['category'])
    create_index_if_missing(
        'bot_memory_facts',
        op.f('ix_bot_memory_facts_source_message_id'),
        ['source_message_id'],
    )


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def drop_index_if_exists(table_name: str, index_name: str) -> None:
        indexes = inspector.get_indexes(table_name, schema=schema_name)
        if not any(index.get('name') == index_name for index in indexes):
            return
        try:
            op.drop_index(index_name, table_name=table_name, schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_not_exists_error(exc):
                raise

    if inspector.has_table('bot_memory_facts', schema=schema_name):
        drop_index_if_exists('bot_memory_facts', op.f('ix_bot_memory_facts_source_message_id'))
        drop_index_if_exists('bot_memory_facts', op.f('ix_bot_memory_facts_category'))
        drop_index_if_exists('bot_memory_facts', op.f('ix_bot_memory_facts_user_id'))
        drop_index_if_exists('bot_memory_facts', op.f('ix_bot_memory_facts_id'))
        op.drop_table('bot_memory_facts', schema=schema_name)

    if inspector.has_table('bot_conversation_messages', schema=schema_name):
        drop_index_if_exists('bot_conversation_messages', op.f('ix_bot_conversation_messages_role'))
        drop_index_if_exists('bot_conversation_messages', op.f('ix_bot_conversation_messages_message_id'))
        drop_index_if_exists('bot_conversation_messages', op.f('ix_bot_conversation_messages_conversation_id'))
        drop_index_if_exists('bot_conversation_messages', op.f('ix_bot_conversation_messages_user_id'))
        drop_index_if_exists('bot_conversation_messages', op.f('ix_bot_conversation_messages_id'))
        op.drop_table('bot_conversation_messages', schema=schema_name)
