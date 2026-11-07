"""Paginated lists: `?page=&size=` (default 25, max 200) -> {items, total, page, size}."""
from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel

T = TypeVar("T")

DEFAULT_SIZE = 25
MAX_SIZE = 200


class PageParams:
    def __init__(self, page: int = Query(1, ge=1), size: int = Query(DEFAULT_SIZE, ge=1, le=MAX_SIZE)):
        self.page = page
        self.size = size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    size: int


def paginate(query, params: PageParams, transform=None) -> dict:
    """Count and slice a SQLAlchemy query. `transform` maps each row to its output."""
    total = query.order_by(None).count()
    rows = query.offset(params.offset).limit(params.size).all()
    items = [transform(r) for r in rows] if transform else rows
    return {"items": items, "total": total, "page": params.page, "size": params.size}
