from decimal import Decimal

from pydantic import BaseModel, HttpUrl


class Product(BaseModel):
    url: HttpUrl
    title: str
    price: Decimal | None = None
    original_price: Decimal | None = None
    rating: float | None = None
    reviews_count: int | None = None
    seller: str | None = None
    in_stock: bool = True
