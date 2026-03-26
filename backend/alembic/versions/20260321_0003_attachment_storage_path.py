"""add storage_path to task attachments

Revision ID: 20260321_0003
Revises: 20260321_0002
Create Date: 2026-03-21 00:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '20260321_0003'
down_revision: Union[str, Sequence[str], None] = '20260321_0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table('task_attachments', schema=schema_name):
        return

    column_names = {column['name'] for column in inspector.get_columns('task_attachments', schema=schema_name)}
    if 'storage_path' in column_names:
        return

    op.add_column('task_attachments', sa.Column('storage_path', sa.String(length=512), nullable=True), schema=schema_name)


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    op.drop_column('task_attachments', 'storage_path', schema=schema_name)
