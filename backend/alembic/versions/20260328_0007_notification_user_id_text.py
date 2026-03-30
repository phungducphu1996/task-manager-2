"""normalize notification_events user_id to varchar

Revision ID: 20260328_0007
Revises: 20260328_0006
Create Date: 2026-03-28 00:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '20260328_0007'
down_revision: Union[str, Sequence[str], None] = '20260328_0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_not_exists_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return 'does not exist' in message or 'undefined' in message


def _qualified_table(schema_name: str | None, table_name: str) -> str:
    if schema_name:
        return f'"{schema_name}"."{table_name}"'
    return f'"{table_name}"'


def upgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table('notification_events', schema=schema_name):
        return

    columns = {column['name']: column for column in inspector.get_columns('notification_events', schema=schema_name)}
    user_id_column = columns.get('user_id')
    if not user_id_column:
        return

    user_id_type = str(user_id_column['type']).lower()
    if 'uuid' not in user_id_type:
        return

    op.execute(
        sa.text(
            f'ALTER TABLE {_qualified_table(schema_name, "notification_events")} '
            'ALTER COLUMN user_id TYPE VARCHAR(64) USING user_id::text'
        )
    )


def downgrade() -> None:
    schema_name = get_settings().normalized_db_schema
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table('notification_events', schema=schema_name):
        return

    columns = {column['name']: column for column in inspector.get_columns('notification_events', schema=schema_name)}
    user_id_column = columns.get('user_id')
    if not user_id_column:
        return

    user_id_type = str(user_id_column['type']).lower()
    if 'character' not in user_id_type and 'varchar' not in user_id_type:
        return

    try:
        op.execute(
            sa.text(
                f'ALTER TABLE {_qualified_table(schema_name, "notification_events")} '
                'ALTER COLUMN user_id TYPE UUID USING user_id::uuid'
            )
        )
    except SQLAlchemyError as exc:
        if not _is_not_exists_error(exc):
            raise
