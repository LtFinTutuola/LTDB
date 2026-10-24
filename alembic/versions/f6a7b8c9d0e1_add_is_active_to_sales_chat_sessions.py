"""add_is_active_to_sales_chat_sessions

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-19 13:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add is_active column to sales_chat_sessions."""
    with op.batch_alter_table('sales_chat_sessions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_active', sa.Boolean(), server_default=sa.text('1'), nullable=False)
        )


def downgrade() -> None:
    """Drop is_active column from sales_chat_sessions."""
    with op.batch_alter_table('sales_chat_sessions', schema=None) as batch_op:
        batch_op.drop_column('is_active')
