"""add_revision_count_to_staging_area

Revision ID: eb96732da96a
Revises: b1c2d3e4f5a6
Create Date: 2026-08-20 10:56:05.458810

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eb96732da96a'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('staging_area', schema=None) as batch_op:
        batch_op.add_column(sa.Column('revision_count', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('staging_area', schema=None) as batch_op:
        batch_op.drop_column('revision_count')

