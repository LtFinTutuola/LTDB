"""Refactor ArticleBlueprint and Article schemas

Revision ID: aeb12f743f82
Revises: 4f6dad1273b9
Create Date: 2026-07-25 10:04:12.412904

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = 'aeb12f743f82'
down_revision: Union[str, Sequence[str], None] = '4f6dad1273b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('article_blueprints', schema=None) as batch_op:
        batch_op.add_column(sa.Column('article_name', sa.String(), nullable=False, server_default='Unknown'))
        batch_op.drop_index(op.f('ix_article_blueprints_ean'))
        batch_op.drop_column('colors')
        batch_op.drop_column('ean')

    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ean', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('colors', sa.JSON(), nullable=True))
        batch_op.drop_index(op.f('ix_articles_sku'))
        batch_op.create_index(op.f('ix_articles_ean'), ['ean'], unique=False)
        batch_op.drop_column('sku')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sku', sa.VARCHAR(), nullable=True))
        batch_op.drop_index(op.f('ix_articles_ean'))
        batch_op.create_index(op.f('ix_articles_sku'), ['sku'], unique=1)
        batch_op.drop_column('colors')
        batch_op.drop_column('ean')

    with op.batch_alter_table('article_blueprints', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ean', sa.VARCHAR(), nullable=True))
        batch_op.add_column(sa.Column('colors', sqlite.JSON(), nullable=True))
        batch_op.create_index(op.f('ix_article_blueprints_ean'), ['ean'], unique=1)
        batch_op.drop_column('article_name')
