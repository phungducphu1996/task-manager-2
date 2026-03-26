"""add task comments and attachments

Revision ID: 20260321_0002
Revises: 20260319_0001
Create Date: 2026-03-21 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '20260321_0002'
down_revision: Union[str, Sequence[str], None] = '20260319_0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    schema_name = get_settings().normalized_db_schema

    bind = op.get_bind()

    def has_table(table: str) -> bool:
        return sa.inspect(bind).has_table(table, schema=schema_name)

    def has_index(table: str, index_name: str) -> bool:
        indexes = sa.inspect(bind).get_indexes(table, schema=schema_name)
        return any(index.get('name') == index_name for index in indexes)

    def is_already_exists_error(exc: Exception) -> bool:
        return 'already exists' in str(exc).lower()

    def safe_create_table(*args, **kwargs) -> None:
        try:
            op.create_table(*args, **kwargs)
        except SQLAlchemyError as exc:
            if not is_already_exists_error(exc):
                raise

    def safe_create_index(index_name: str, table_name: str, columns: list[str]) -> None:
        try:
            op.create_index(index_name, table_name, columns, unique=False, schema=schema_name)
        except SQLAlchemyError as exc:
            if not is_already_exists_error(exc):
                raise

    if not has_table('task_comments'):
        safe_create_table(
            'task_comments',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('task_id', sa.Integer(), nullable=False),
            sa.Column('author_id', sa.Uuid(as_uuid=False), nullable=True),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('mentions', sa.JSON(), nullable=False, server_default='[]'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['task_id'], [f'{schema_name}.tasks.id' if schema_name else 'tasks.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )

    ix_task_comments_id = op.f('ix_task_comments_id')
    if has_table('task_comments') and not has_index('task_comments', ix_task_comments_id):
        safe_create_index(ix_task_comments_id, 'task_comments', ['id'])

    ix_task_comments_task_id = op.f('ix_task_comments_task_id')
    if has_table('task_comments') and not has_index('task_comments', ix_task_comments_task_id):
        safe_create_index(ix_task_comments_task_id, 'task_comments', ['task_id'])

    if not has_table('task_attachments'):
        safe_create_table(
            'task_attachments',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('task_id', sa.Integer(), nullable=False),
            sa.Column('uploaded_by', sa.Uuid(as_uuid=False), nullable=True),
            sa.Column('name', sa.String(length=255), nullable=False),
            sa.Column('mime_type', sa.String(length=255), nullable=False),
            sa.Column('size_bytes', sa.Integer(), nullable=False),
            sa.Column('data_url', sa.Text(), nullable=False),
            sa.Column('is_image', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['task_id'], [f'{schema_name}.tasks.id' if schema_name else 'tasks.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )

    ix_task_attachments_id = op.f('ix_task_attachments_id')
    if has_table('task_attachments') and not has_index('task_attachments', ix_task_attachments_id):
        safe_create_index(ix_task_attachments_id, 'task_attachments', ['id'])

    ix_task_attachments_task_id = op.f('ix_task_attachments_task_id')
    if has_table('task_attachments') and not has_index('task_attachments', ix_task_attachments_task_id):
        safe_create_index(ix_task_attachments_task_id, 'task_attachments', ['task_id'])


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema

    op.drop_index(op.f('ix_task_attachments_task_id'), table_name='task_attachments', schema=schema_name)
    op.drop_index(op.f('ix_task_attachments_id'), table_name='task_attachments', schema=schema_name)
    op.drop_table('task_attachments', schema=schema_name)

    op.drop_index(op.f('ix_task_comments_task_id'), table_name='task_comments', schema=schema_name)
    op.drop_index(op.f('ix_task_comments_id'), table_name='task_comments', schema=schema_name)
    op.drop_table('task_comments', schema=schema_name)
