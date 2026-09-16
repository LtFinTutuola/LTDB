from typing import List, Optional
from sqlalchemy import String, ForeignKey, JSON, Boolean, LargeBinary, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, UUIDMixin, TimestampMixin
from src.models.types import LowercaseJSONList


class BrandHeuristic(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "brand_heuristics"

    brand_id: Mapped[str] = mapped_column(String(36), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False)
    pattern: Mapped[str] = mapped_column(String, nullable=False)
    explanation: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Relationships
    brand: Mapped["Brand"] = relationship("Brand", back_populates="heuristics")


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
        UniqueConstraint("brand_id", "normalized_vendor_code", name="uq_brand_normalized_vendor_code"),
    )

    brand_id: Mapped[str] = mapped_column(String(36), ForeignKey("brands.id"), nullable=False)
    category_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("categories.id"), nullable=True)
    normalized_vendor_code: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    
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
    articles: Mapped[List["Article"]] = relationship("Article", back_populates="blueprint")
    photos: Mapped[List["ArticlePhoto"]] = relationship("ArticlePhoto", back_populates="blueprint", cascade="all, delete-orphan")


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
