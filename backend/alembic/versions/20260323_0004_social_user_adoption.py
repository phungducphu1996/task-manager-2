"""adopt social users and remove local user foreign keys

Revision ID: 20260323_0004
Revises: 20260321_0003
Create Date: 2026-03-23 00:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '20260323_0004'
down_revision: Union[str, Sequence[str], None] = '20260321_0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    targets: list[tuple[str, str]] = [
        ('tasks', 'assigned_to'),
        ('tasks', 'created_by'),
        ('task_comments', 'author_id'),
        ('task_attachments', 'uploaded_by'),
    ]

    for table_name, column_name in targets:
        if not inspector.has_table(table_name, schema=schema_name):
            continue

        foreign_keys = inspector.get_foreign_keys(table_name, schema=schema_name)
        for foreign_key in foreign_keys:
            constraint_name = foreign_key.get('name')
            constrained_columns = foreign_key.get('constrained_columns') or []
            referred_table = foreign_key.get('referred_table')

            if not constraint_name:
                continue
            if column_name not in constrained_columns:
                continue
            if referred_table != 'users':
                continue

            op.drop_constraint(constraint_name, table_name, type_='foreignkey', schema=schema_name)


def downgrade() -> None:
    # We intentionally keep this migration one-way because re-attaching local users
    # foreign keys can fail when task rows already reference social user IDs.
    pass

