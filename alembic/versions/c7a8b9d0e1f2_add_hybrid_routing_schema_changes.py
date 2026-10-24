"""add hybrid routing schema changes

Revision ID: c7a8b9d0e1f2
Revises: eb96732da96a
Create Date: 2026-08-26 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7a8b9d0e1f2'
down_revision: Union[str, None] = 'ac1496c3b2a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- Brand table: add heuristic columns --
    op.add_column('brands', sa.Column('brand_code_heuristic', sa.String(), nullable=True))
    op.add_column('brands', sa.Column('brand_code_explanation', sa.String(), nullable=True))
    op.add_column('brands', sa.Column('heuristic_confirmed', sa.Boolean(), nullable=False, server_default=sa.text('0')))

    # -- ArticleBlueprint table: add normalized_vendor_code --
    op.add_column('article_blueprints', sa.Column('normalized_vendor_code', sa.String(), nullable=True))
    op.create_index('ix_article_blueprints_normalized_vendor_code', 'article_blueprints', ['normalized_vendor_code'])
    with op.batch_alter_table('article_blueprints') as batch_op:
        batch_op.create_unique_constraint('uq_brand_normalized_vendor_code', ['brand_id', 'normalized_vendor_code'])

    # -- StagingArea table: add job_type, make file_path nullable --
    op.add_column('staging_area', sa.Column('job_type', sa.String(), nullable=False, server_default='ddt_import'))
    with op.batch_alter_table('staging_area') as batch_op:
        batch_op.alter_column('file_path', existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    # -- StagingArea: revert --
    with op.batch_alter_table('staging_area') as batch_op:
        batch_op.alter_column('file_path', existing_type=sa.String(), nullable=False)
        batch_op.drop_column('job_type')

    # -- ArticleBlueprint: revert --
    with op.batch_alter_table('article_blueprints') as batch_op:
        batch_op.drop_constraint('uq_brand_normalized_vendor_code', type_='unique')
        batch_op.drop_index('ix_article_blueprints_normalized_vendor_code')
        batch_op.drop_column('normalized_vendor_code')

    # -- Brand: revert --
    with op.batch_alter_table('brands') as batch_op:
        batch_op.drop_column('heuristic_confirmed')
        batch_op.drop_column('brand_code_explanation')
        batch_op.drop_column('brand_code_heuristic')
