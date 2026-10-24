"""add_assistant_indexes

Revision ID: c1367eeda2a2
Revises: abd33fef6684
Create Date: 2026-10-09 17:07:28.647547

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1367eeda2a2'
down_revision: Union[str, Sequence[str], None] = 'abd33fef6684'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index('ix_articles_blueprint_status', 'articles', ['article_blueprint_id', 'status'])
    op.create_index('ix_categories_parent_id', 'categories', ['parent_id'])
    op.create_index('ix_article_movements_article_id', 'article_movements', ['article_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_articles_blueprint_status', table_name='articles')
    op.drop_index('ix_categories_parent_id', table_name='categories')
    op.drop_index('ix_article_movements_article_id', table_name='article_movements')
