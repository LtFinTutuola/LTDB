"""add heuristic mode, confirmed and metadata to brand_heuristics

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
"""
from alembic import op
import sqlalchemy as sa

revision = "b7c8d9e0f1a2"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("brand_heuristics") as batch:
        batch.add_column(sa.Column("heuristic_mode", sa.String(), nullable=False, server_default="PENDING_COHORT"))
        batch.add_column(sa.Column("heuristic_confirmed", sa.Boolean(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("heuristic_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("brand_heuristics") as batch:
        batch.drop_column("heuristic_metadata")
        batch.drop_column("heuristic_confirmed")
        batch.drop_column("heuristic_mode")
