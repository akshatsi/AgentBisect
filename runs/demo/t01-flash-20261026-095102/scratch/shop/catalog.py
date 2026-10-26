"""Products and the catalog that holds them."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace

from shop.errors import DuplicateProductError, UnknownProductError


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price_cents: int
    category: str = "general"
    taxable: bool = True
    weight_g: int = 0

    def __post_init__(self) -> None:
        if not self.sku:
            raise ValueError("sku must not be empty")
        if self.price_cents < 0:
            raise ValueError("price must not be negative")
        if self.weight_g < 0:
            raise ValueError("weight must not be negative")


class Catalog:
    def __init__(self, products: Iterable[Product] = ()) -> None:
        self._products: dict[str, Product] = {}
        for product in products:
            self.add(product)

    def add(self, product: Product) -> None:
        if product.sku in self._products:
            raise DuplicateProductError(product.sku)
        self._products[product.sku] = product

    def get(self, sku: str) -> Product:
        try:
            return self._products[sku]
        except KeyError:
            raise UnknownProductError(sku) from None

    def update_price(self, sku: str, price_cents: int) -> Product:
        """Set the price for ``sku`` and return the new product.

        Orders already placed keep the price they were placed at.
        """
        product = self.get(sku)
        if price_cents < 0:
            raise ValueError("price must not be negative")
        updated = replace(product, price_cents=price_cents)
        self._products[sku] = updated
        return updated

    def remove(self, sku: str) -> Product:
        product = self.get(sku)
        del self._products[sku]
        return product

    def skus(self) -> list[str]:
        return sorted(self._products)

    def by_category(self, category: str) -> list[Product]:
        return [p for p in self._sorted() if p.category == category]

    def categories(self) -> list[str]:
        return sorted({p.category for p in self._products.values()})

    def search(self, text: str) -> list[Product]:
        """Products whose name contains ``text``, ordered by SKU."""
        return [p for p in self._sorted() if text in p.name]

    def _sorted(self) -> list[Product]:
        return [self._products[sku] for sku in sorted(self._products)]

    def __contains__(self, sku: object) -> bool:
        return sku in self._products

    def __len__(self) -> int:
        return len(self._products)

    def __iter__(self) -> Iterator[Product]:
        return iter(self._sorted())
