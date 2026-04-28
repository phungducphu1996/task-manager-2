"""add bot conversation state

Revision ID: 20260427_0010
Revises: 20260424_0009
Create Date: 2026-04-27 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '20260427_0010'
down_revision: Union[str, Sequence[str], None] = '20260424_0009'
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

    if not inspector.has_table('bot_conversation_states', schema=schema_name):
        op.create_table(
            'bot_conversation_states',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.String(length=64), nullable=True),
            sa.Column('conversation_id', sa.String(length=128), nullable=False),
            sa.Column('state', sa.JSON(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('conversation_id'),
            schema=schema_name,
        )

    inspector = sa.inspect(bind)

    def has_index(index_name: str) -> bool:
        indexes = inspector.get_indexes('bot_conversation_states', schema=schema_name)
        return any(index.get('name') == index_name for index in indexes)

    def create_index_if_missing(index_name: str, columns: list[str]) -> None:
        if has_index(index_name):
            return
        try:
            op.create_index(index_name, 'bot_conversation_states', columns, unique=False, schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_already_exists_error(exc):
                raise

    create_index_if_missing(op.f('ix_bot_conversation_states_id'), ['id'])
    create_index_if_missing(op.f('ix_bot_conversation_states_user_id'), ['user_id'])
    create_index_if_missing(op.f('ix_bot_conversation_states_conversation_id'), ['conversation_id'])


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table('bot_conversation_states', schema=schema_name):
        return

    def drop_index_if_exists(index_name: str) -> None:
        indexes = inspector.get_indexes('bot_conversation_states', schema=schema_name)
        if not any(index.get('name') == index_name for index in indexes):
            return
        try:
            op.drop_index(index_name, table_name='bot_conversation_states', schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_not_exists_error(exc):
                raise

    drop_index_if_exists(op.f('ix_bot_conversation_states_conversation_id'))
    drop_index_if_exists(op.f('ix_bot_conversation_states_user_id'))
    drop_index_if_exists(op.f('ix_bot_conversation_states_id'))
    op.drop_table('bot_conversation_states', schema=schema_name)
