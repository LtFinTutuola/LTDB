from typing import List, Optional
from datetime import datetime
from sqlalchemy import String, ForeignKey, JSON, Boolean, LargeBinary, UniqueConstraint, Index, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, UUIDMixin, TimestampMixin
from src.models.types import LowercaseJSONList


class HeuristicMode:
    """Modalità operative della euristica di normalizzazione di un brand."""
    PENDING_COHORT = "PENDING_COHORT"
    COLOR_MASKED = "COLOR_MASKED"
    COLOR_MASKED_COMPACT = "COLOR_MASKED_COMPACT"
    COLOR_MASKED_DELIMITED = "COLOR_MASKED_DELIMITED"
    COLOR_AGNOSTIC = "COLOR_AGNOSTIC"

    ALL = (
        PENDING_COHORT,
        COLOR_MASKED,
        COLOR_MASKED_COMPACT,
        COLOR_MASKED_DELIMITED,
        COLOR_AGNOSTIC,
    )
    MASKED_MODES = {COLOR_MASKED, COLOR_MASKED_COMPACT, COLOR_MASKED_DELIMITED}


class BrandHeuristic(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "brand_heuristics"

    brand_id: Mapped[str] = mapped_column(String(36), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False)
    pattern: Mapped[str] = mapped_column(String, nullable=False, default="")
    explanation: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    heuristic_mode: Mapped[str] = mapped_column(String, nullable=False, default=HeuristicMode.PENDING_COHORT, server_default=HeuristicMode.PENDING_COHORT)
    heuristic_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    color_segment_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # {"rules": [...], "negative_constraints": [[codeA, codeB], ...]}
    heuristic_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Relationships
    brand: Mapped["Brand"] = relationship("Brand", back_populates="heuristics")
    blueprints: Mapped[List["ArticleBlueprint"]] = relationship("ArticleBlueprint", back_populates="heuristic")
    code_embeddings: Mapped[List["CodeEmbedding"]] = relationship("CodeEmbedding", back_populates="heuristic", cascade="all, delete-orphan")


class Brand(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "brands"

    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    # Relationships
    heuristics: Mapped[List["BrandHeuristic"]] = relationship("BrandHeuristic", back_populates="brand", cascade="all, delete-orphan")
    blueprints: Mapped[List["ArticleBlueprint"]] = relationship("ArticleBlueprint", back_populates="brand")
    categories: Mapped[List["Category"]] = relationship("Category", secondary="brand_categories", back_populates="brands")


class Category(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "categories"

    parent_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("categories.id"), nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)

    # Adjacency List relationships
    subcategories: Mapped[List["Category"]] = relationship(
        "Category",
        backref="parent",
        remote_side="[Category.id]"
    )
    
    # Relationships
    brands: Mapped[List["Brand"]] = relationship("Brand", secondary="brand_categories", back_populates="categories")
    blueprints: Mapped[List["ArticleBlueprint"]] = relationship("ArticleBlueprint", back_populates="category")


class BrandCategory(Base):
    __tablename__ = "brand_categories"

    brand_id: Mapped[str] = mapped_column(String(36), ForeignKey("brands.id"), primary_key=True)
    category_id: Mapped[str] = mapped_column(String(36), ForeignKey("categories.id"), primary_key=True)


class ArticleBlueprint(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "article_blueprints"
    __table_args__ = (
        Index("ix_brand_skeleton_vendor_code", "brand_id", "skeleton_vendor_code"),
        Index("ix_article_blueprints_created_at", "created_at"),
    )

    brand_id: Mapped[str] = mapped_column(String(36), ForeignKey("brands.id"), nullable=False, index=True)
    category_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("categories.id"), nullable=True, index=True)
    heuristic_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("brand_heuristics.id", ondelete="SET NULL"), nullable=True, index=True)
    normalized_vendor_code: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    skeleton_vendor_code: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    
    article_name: Mapped[str] = mapped_column(String, nullable=False, default="Unknown")
    
    description: Mapped[str] = mapped_column(String, nullable=False)
    extended_description: Mapped[str] = mapped_column(String, nullable=False)
    dimensions: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    
    # JSON arrays for SQLite (using JSON1 extension via SQLAlchemy JSON)
    tags: Mapped[list] = mapped_column(LowercaseJSONList, nullable=False)
    materials: Mapped[Optional[list]] = mapped_column(LowercaseJSONList, nullable=True)
    embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Relationships
    brand: Mapped["Brand"] = relationship("Brand", back_populates="blueprints")
    category: Mapped[Optional["Category"]] = relationship("Category", back_populates="blueprints")
    heuristic: Mapped[Optional["BrandHeuristic"]] = relationship("BrandHeuristic", back_populates="blueprints")
    articles: Mapped[List["Article"]] = relationship("Article", back_populates="blueprint")
    photos: Mapped[List["ArticlePhoto"]] = relationship("ArticlePhoto", back_populates="blueprint", cascade="all, delete-orphan")


class CodeEmbedding(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "code_embeddings"
    __table_args__ = (
        Index("ix_code_embeddings_heuristic_id", "heuristic_id"),
        Index("ix_code_embeddings_brand_id", "brand_id"),
    )

    heuristic_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("brand_heuristics.id", ondelete="CASCADE"), nullable=False
    )
    brand_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    total_length_min: Mapped[int] = mapped_column(Integer, nullable=False)
    total_length_max: Mapped[int] = mapped_column(Integer, nullable=False)
    root_length: Mapped[int] = mapped_column(Integer, nullable=False)
    color_length_min: Mapped[int] = mapped_column(Integer, nullable=False)
    color_length_max: Mapped[int] = mapped_column(Integer, nullable=False)
    separator_count: Mapped[int] = mapped_column(Integer, nullable=False)
    root_blocks_pattern: Mapped[str] = mapped_column(String, nullable=False)
    root_prefix_anchor: Mapped[str] = mapped_column(String, nullable=False)
    sample_raw_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Relationships
    heuristic: Mapped["BrandHeuristic"] = relationship("BrandHeuristic", back_populates="code_embeddings")
    brand: Mapped["Brand"] = relationship("Brand")


class ArticlePhoto(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "article_photos"
    __table_args__ = (
        UniqueConstraint("article_blueprint_id", "canonical_color_name",
                         name="uq_blueprint_color_photo"),
    )

    article_blueprint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("article_blueprints.id", ondelete="CASCADE"), nullable=False
    )
    canonical_color_name: Mapped[str] = mapped_column(String, nullable=False)
    photo_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Relationships
    blueprint: Mapped["ArticleBlueprint"] = relationship("ArticleBlueprint", back_populates="photos")
