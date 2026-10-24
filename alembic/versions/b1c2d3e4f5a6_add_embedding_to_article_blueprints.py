"""add embedding to article_blueprints

Revision ID: b1c2d3e4f5a6
Revises: 823081f1fa5e
Create Date: 2026-07-27 21:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = '823081f1fa5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('article_blueprints', schema=None) as batch_op:
        batch_op.add_column(sa.Column('embedding', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('article_blueprints', schema=None) as batch_op:
        batch_op.drop_column('embedding')
