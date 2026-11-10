"""add catalog performance indexes

Revision ID: d1e2f3a4b5c6
Revises: c8d9e0f1a2b3
"""
from alembic import op
import sqlalchemy as sa

revision = "d1e2f3a4b5c6"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Indexes on 'articles' table
    art_indexes = {idx["name"] for idx in inspector.get_indexes("articles")}
    if "ix_articles_article_blueprint_id" not in art_indexes:
        op.create_index("ix_articles_article_blueprint_id", "articles", ["article_blueprint_id"])
    if "ix_articles_batch_id" not in art_indexes:
        op.create_index("ix_articles_batch_id", "articles", ["batch_id"])
    if "ix_articles_status" not in art_indexes:
        op.create_index("ix_articles_status", "articles", ["status"])
    if "ix_articles_bp_status" not in art_indexes:
        op.create_index("ix_articles_bp_status", "articles", ["article_blueprint_id", "status"])
    if "ix_articles_created_at" not in art_indexes:
        op.create_index("ix_articles_created_at", "articles", ["created_at"])

    # 2. Indexes on 'article_blueprints' table
    bp_indexes = {idx["name"] for idx in inspector.get_indexes("article_blueprints")}
    if "ix_article_blueprints_brand_id" not in bp_indexes:
        op.create_index("ix_article_blueprints_brand_id", "article_blueprints", ["brand_id"])
    if "ix_article_blueprints_category_id" not in bp_indexes:
        op.create_index("ix_article_blueprints_category_id", "article_blueprints", ["category_id"])
    if "ix_article_blueprints_created_at" not in bp_indexes:
        op.create_index("ix_article_blueprints_created_at", "article_blueprints", ["created_at"])

    # 3. Indexes on 'batches' table
    batch_indexes = {idx["name"] for idx in inspector.get_indexes("batches")}
    if "ix_batches_document_date" not in batch_indexes:
        op.create_index("ix_batches_document_date", "batches", ["document_date"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    art_indexes = {idx["name"] for idx in inspector.get_indexes("articles")}
    for idx_name in [
        "ix_articles_bp_status",
        "ix_articles_created_at",
        "ix_articles_status",
        "ix_articles_batch_id",
        "ix_articles_article_blueprint_id",
    ]:
        if idx_name in art_indexes:
            op.drop_index(idx_name, table_name="articles")

    bp_indexes = {idx["name"] for idx in inspector.get_indexes("article_blueprints")}
    for idx_name in [
        "ix_article_blueprints_created_at",
        "ix_article_blueprints_category_id",
        "ix_article_blueprints_brand_id",
    ]:
        if idx_name in bp_indexes:
            op.drop_index(idx_name, table_name="article_blueprints")

    batch_indexes = {idx["name"] for idx in inspector.get_indexes("batches")}
    if "ix_batches_document_date" in batch_indexes:
        op.drop_index("ix_batches_document_date", table_name="batches")
