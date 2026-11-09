"""add code embeddings, skeleton code and color segment index

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
"""
import re
from alembic import op
import sqlalchemy as sa

revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Update brand_heuristics (add color_segment_index if not present)
    bh_cols = [c["name"] for c in inspector.get_columns("brand_heuristics")]
    if "color_segment_index" not in bh_cols:
        with op.batch_alter_table("brand_heuristics") as batch:
            batch.add_column(sa.Column("color_segment_index", sa.Integer(), nullable=True))

    # 2. Create code_embeddings table (if not exists)
    tables = inspector.get_table_names()
    if "code_embeddings" not in tables:
        op.create_table(
            "code_embeddings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("heuristic_id", sa.String(36), sa.ForeignKey("brand_heuristics.id", ondelete="CASCADE"), nullable=False),
            sa.Column("brand_id", sa.String(36), sa.ForeignKey("brands.id", ondelete="CASCADE"), nullable=False),
            sa.Column("total_length_min", sa.Integer(), nullable=False),
            sa.Column("total_length_max", sa.Integer(), nullable=False),
            sa.Column("root_length", sa.Integer(), nullable=False),
            sa.Column("color_length_min", sa.Integer(), nullable=False),
            sa.Column("color_length_max", sa.Integer(), nullable=False),
            sa.Column("separator_count", sa.Integer(), nullable=False),
            sa.Column("root_blocks_pattern", sa.String(), nullable=False),
            sa.Column("root_prefix_anchor", sa.String(), nullable=False),
            sa.Column("sample_raw_code", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        )
        op.create_index("ix_code_embeddings_heuristic_id", "code_embeddings", ["heuristic_id"])
        op.create_index("ix_code_embeddings_brand_id", "code_embeddings", ["brand_id"])

    # 3. Update article_blueprints
    bp_cols = [c["name"] for c in inspector.get_columns("article_blueprints")]
    with op.batch_alter_table("article_blueprints") as batch:
        if "heuristic_id" not in bp_cols:
            batch.add_column(
                sa.Column(
                    "heuristic_id",
                    sa.String(36),
                    sa.ForeignKey("brand_heuristics.id", name="fk_article_blueprints_heuristic_id", ondelete="SET NULL"),
                    nullable=True,
                )
            )
            batch.create_index("ix_article_blueprints_heuristic_id", ["heuristic_id"])
        if "skeleton_vendor_code" not in bp_cols:
            batch.add_column(sa.Column("skeleton_vendor_code", sa.String(), nullable=True))
            batch.create_index("ix_article_blueprints_skeleton_vendor_code", ["skeleton_vendor_code"])
            batch.create_index("ix_brand_skeleton_vendor_code", ["brand_id", "skeleton_vendor_code"])
        try:
            batch.drop_constraint("uq_brand_normalized_vendor_code", type_="unique")
        except Exception:
            pass

    # 4. Populate skeleton_vendor_code from normalized_vendor_code
    blueprints = bind.execute(sa.text("SELECT id, normalized_vendor_code FROM article_blueprints WHERE normalized_vendor_code IS NOT NULL")).fetchall()
    for row in blueprints:
        bp_id = row[0]
        nvc = row[1]
        if nvc:
            skeleton = re.sub(r"[@#\s\-/\\|._*]+", "", nvc.strip().upper())
            bind.execute(
                sa.text("UPDATE article_blueprints SET skeleton_vendor_code = :skeleton WHERE id = :id"),
                {"skeleton": skeleton, "id": bp_id}
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    bp_cols = [c["name"] for c in inspector.get_columns("article_blueprints")]
    with op.batch_alter_table("article_blueprints") as batch:
        try:
            batch.drop_index("ix_brand_skeleton_vendor_code")
        except Exception:
            pass
        try:
            batch.drop_index("ix_article_blueprints_skeleton_vendor_code")
        except Exception:
            pass
        try:
            batch.drop_index("ix_article_blueprints_heuristic_id")
        except Exception:
            pass
        if "skeleton_vendor_code" in bp_cols:
            batch.drop_column("skeleton_vendor_code")
        if "heuristic_id" in bp_cols:
            batch.drop_column("heuristic_id")
        try:
            batch.create_unique_constraint("uq_brand_normalized_vendor_code", ["brand_id", "normalized_vendor_code"])
        except Exception:
            pass

    tables = inspector.get_table_names()
    if "code_embeddings" in tables:
        try:
            op.drop_index("ix_code_embeddings_brand_id", table_name="code_embeddings")
        except Exception:
            pass
        try:
            op.drop_index("ix_code_embeddings_heuristic_id", table_name="code_embeddings")
        except Exception:
            pass
        op.drop_table("code_embeddings")

    bh_cols = [c["name"] for c in inspector.get_columns("brand_heuristics")]
    if "color_segment_index" in bh_cols:
        with op.batch_alter_table("brand_heuristics") as batch:
            batch.drop_column("color_segment_index")
