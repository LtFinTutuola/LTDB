from typing import List, Optional
from sqlalchemy import String, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, UUIDMixin, TimestampMixin


class Brand(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "brands"

    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    # Relationships
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

    brand_id: Mapped[str] = mapped_column(String(36), ForeignKey("brands.id"), nullable=False)
    category_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("categories.id"), nullable=True)
    
    supplier_code: Mapped[str] = mapped_column(String, index=True, nullable=False)
    ean: Mapped[Optional[str]] = mapped_column(String, unique=True, index=True, nullable=True)
    
    description: Mapped[str] = mapped_column(String, nullable=False)
    extended_description: Mapped[str] = mapped_column(String, nullable=False)
    
    # JSON arrays for SQLite (using JSON1 extension via SQLAlchemy JSON)
    tags: Mapped[list] = mapped_column(JSON, nullable=False)
    colors: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    materials: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Relationships
    brand: Mapped["Brand"] = relationship("Brand", back_populates="blueprints")
    category: Mapped[Optional["Category"]] = relationship("Category", back_populates="blueprints")
    articles: Mapped[List["Article"]] = relationship("Article", back_populates="blueprint")
