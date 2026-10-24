"""add_reporting_to_sales_chat_messages

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-19 11:42:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add is_reported and report_reason columns to sales_chat_messages."""
    with op.batch_alter_table('sales_chat_messages', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_reported', sa.Boolean(), server_default=sa.text('0'), nullable=False)
        )
        batch_op.add_column(
            sa.Column('report_reason', sa.Text(), nullable=True)
        )


def downgrade() -> None:
    """Drop is_reported and report_reason columns from sales_chat_messages."""
    with op.batch_alter_table('sales_chat_messages', schema=None) as batch_op:
        batch_op.drop_column('report_reason')
        batch_op.drop_column('is_reported')
