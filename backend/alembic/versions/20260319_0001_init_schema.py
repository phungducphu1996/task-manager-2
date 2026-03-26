"""init schema

Revision ID: 20260319_0001
Revises: 
Create Date: 2026-03-19 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '20260319_0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    schema_name = get_settings().normalized_db_schema

    def ref(table: str, column: str) -> str:
        if schema_name:
            return f'{schema_name}.{table}.{column}'
        return f'{table}.{column}'

    task_status_type = postgresql.ENUM('todo', 'doing', 'review', 'ready', 'done', name='task_status', schema=schema_name)
    task_priority_type = postgresql.ENUM('low', 'medium', 'high', 'urgent', name='task_priority', schema=schema_name)

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

    task_status_type.create(bind, checkfirst=True)
    task_priority_type.create(bind, checkfirst=True)

    if not has_table('users'):
        safe_create_table(
            'users',
            sa.Column('id', sa.Uuid(as_uuid=False), nullable=False),
            sa.Column('username', sa.String(length=120), nullable=False),
            sa.Column('password_hash', sa.String(length=255), nullable=True),
            sa.Column('role', sa.String(length=50), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )
    ix_users_id = op.f('ix_users_id')
    if has_table('users') and not has_index('users', ix_users_id):
        safe_create_index(ix_users_id, 'users', ['id'])

    if not has_table('shops'):
        safe_create_table(
            'shops',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('name'),
            schema=schema_name,
        )
    ix_shops_id = op.f('ix_shops_id')
    if has_table('shops') and not has_index('shops', ix_shops_id):
        safe_create_index(ix_shops_id, 'shops', ['id'])

    if not has_table('task_types'):
        safe_create_table(
            'task_types',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('name'),
            schema=schema_name,
        )
    ix_task_types_id = op.f('ix_task_types_id')
    if has_table('task_types') and not has_index('task_types', ix_task_types_id):
        safe_create_index(ix_task_types_id, 'task_types', ['id'])

    if not has_table('tasks'):
        safe_create_table(
            'tasks',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(length=255), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column(
                'status',
                postgresql.ENUM(
                    'todo',
                    'doing',
                    'review',
                    'ready',
                    'done',
                    name='task_status',
                    schema=schema_name,
                    create_type=False,
                ),
                nullable=False,
                server_default='todo',
            ),
            sa.Column('assigned_to', sa.Uuid(as_uuid=False), nullable=True),
            sa.Column('created_by', sa.Uuid(as_uuid=False), nullable=True),
            sa.Column('shop_id', sa.Integer(), nullable=True),
            sa.Column('type_id', sa.Integer(), nullable=True),
            sa.Column('scheduled_date', sa.Date(), nullable=True),
            sa.Column('due_date', sa.Date(), nullable=True),
            sa.Column(
                'priority',
                postgresql.ENUM(
                    'low',
                    'medium',
                    'high',
                    'urgent',
                    name='task_priority',
                    schema=schema_name,
                    create_type=False,
                ),
                nullable=False,
                server_default='medium',
            ),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('is_someday', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('list_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['assigned_to'], [ref('users', 'id')]),
            sa.ForeignKeyConstraint(['created_by'], [ref('users', 'id')]),
            sa.ForeignKeyConstraint(['shop_id'], [ref('shops', 'id')]),
            sa.ForeignKeyConstraint(['type_id'], [ref('task_types', 'id')]),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )
    ix_tasks_id = op.f('ix_tasks_id')
    if has_table('tasks') and not has_index('tasks', ix_tasks_id):
        safe_create_index(ix_tasks_id, 'tasks', ['id'])

    if not has_table('subtasks'):
        safe_create_table(
            'subtasks',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('task_id', sa.Integer(), nullable=False),
            sa.Column('content', sa.String(length=255), nullable=False),
            sa.Column('is_done', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
            sa.ForeignKeyConstraint(['task_id'], [ref('tasks', 'id')], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            schema=schema_name,
        )
    ix_subtasks_id = op.f('ix_subtasks_id')
    if has_table('subtasks') and not has_index('subtasks', ix_subtasks_id):
        safe_create_index(ix_subtasks_id, 'subtasks', ['id'])
    ix_subtasks_task_id = op.f('ix_subtasks_task_id')
    if has_table('subtasks') and not has_index('subtasks', ix_subtasks_task_id):
        safe_create_index(ix_subtasks_task_id, 'subtasks', ['task_id'])


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema

    op.drop_index(op.f('ix_subtasks_task_id'), table_name='subtasks', schema=schema_name)
    op.drop_index(op.f('ix_subtasks_id'), table_name='subtasks', schema=schema_name)
    op.drop_table('subtasks', schema=schema_name)

    op.drop_index(op.f('ix_tasks_id'), table_name='tasks', schema=schema_name)
    op.drop_table('tasks', schema=schema_name)

    op.drop_index(op.f('ix_task_types_id'), table_name='task_types', schema=schema_name)
    op.drop_table('task_types', schema=schema_name)

    op.drop_index(op.f('ix_shops_id'), table_name='shops', schema=schema_name)
    op.drop_table('shops', schema=schema_name)

    op.drop_index(op.f('ix_users_id'), table_name='users', schema=schema_name)
    op.drop_table('users', schema=schema_name)

    task_priority = postgresql.ENUM('low', 'medium', 'high', 'urgent', name='task_priority', schema=schema_name)
    task_status = postgresql.ENUM('todo', 'doing', 'review', 'ready', 'done', name='task_status', schema=schema_name)
    bind = op.get_bind()
    task_priority.drop(bind, checkfirst=True)
    task_status.drop(bind, checkfirst=True)
