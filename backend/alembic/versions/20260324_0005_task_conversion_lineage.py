"""add task conversion lineage column

Revision ID: 20260324_0005
Revises: 20260323_0004
Create Date: 2026-03-24 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '20260324_0005'
down_revision: Union[str, Sequence[str], None] = '20260323_0004'
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

    if not inspector.has_table('tasks', schema=schema_name):
        return

    task_columns = {column['name'] for column in inspector.get_columns('tasks', schema=schema_name)}
    if 'parent_task_id' not in task_columns:
        try:
            op.add_column('tasks', sa.Column('parent_task_id', sa.Integer(), nullable=True), schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_already_exists_error(exc):
                raise

    inspector = sa.inspect(bind)
    fk_exists = any(
        'parent_task_id' in (foreign_key.get('constrained_columns') or [])
        for foreign_key in inspector.get_foreign_keys('tasks', schema=schema_name)
    )
    if not fk_exists:
        try:
            op.create_foreign_key(
                op.f('fk_tasks_parent_task_id_tasks'),
                source_table='tasks',
                referent_table='tasks',
                local_cols=['parent_task_id'],
                remote_cols=['id'],
                source_schema=schema_name,
                referent_schema=schema_name,
                ondelete='SET NULL',
            )
        except SQLAlchemyError as exc:
            if not _is_already_exists_error(exc):
                raise

    indexes = inspector.get_indexes('tasks', schema=schema_name)
    index_name = op.f('ix_tasks_parent_task_id')
    if not any(index.get('name') == index_name for index in indexes):
        try:
            op.create_index(index_name, 'tasks', ['parent_task_id'], unique=False, schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_already_exists_error(exc):
                raise


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table('tasks', schema=schema_name):
        return

    index_name = op.f('ix_tasks_parent_task_id')
    try:
        op.drop_index(index_name, table_name='tasks', schema=schema_name)
    except SQLAlchemyError as exc:
        if not _is_not_exists_error(exc):
            raise

    for foreign_key in inspector.get_foreign_keys('tasks', schema=schema_name):
        constrained = set(foreign_key.get('constrained_columns') or [])
        name = foreign_key.get('name')
        if 'parent_task_id' in constrained and name:
            try:
                op.drop_constraint(name, 'tasks', type_='foreignkey', schema=schema_name)
            except SQLAlchemyError as exc:
                if not _is_not_exists_error(exc):
                    raise
            break

    task_columns = {column['name'] for column in inspector.get_columns('tasks', schema=schema_name)}
    if 'parent_task_id' in task_columns:
        try:
            op.drop_column('tasks', 'parent_task_id', schema=schema_name)
        except SQLAlchemyError as exc:
            if not _is_not_exists_error(exc):
                raise
