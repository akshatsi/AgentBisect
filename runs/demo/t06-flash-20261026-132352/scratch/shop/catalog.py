"""Products and the catalog that holds them."""

from __future__ import annotations

from bisect import bisect_left, insort
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

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


def _sku(product: Product) -> str:
    return product.sku


class Catalog:
    def __init__(self, products: Iterable[Product] = ()) -> None:
        self._products: dict[str, Product] = {}
        # Category -> products sorted by SKU, kept in step by add/remove.
        self._by_category: dict[str, list[Product]] = {}
        for product in products:
            self.add(product)

    def add(self, product: Product) -> None:
        if product.sku in self._products:
            raise DuplicateProductError(product.sku)
        self._products[product.sku] = product
        insort(
            self._by_category.setdefault(product.category, []),
            product,
            key=_sku,
        )

    def get(self, sku: str) -> Product:
        try:
            return self._products[sku]
        except KeyError:
            raise UnknownProductError(sku) from None

    def remove(self, sku: str) -> Product:
        product = self.get(sku)
        del self._products[sku]
        bucket = self._by_category[product.category]
        del bucket[bisect_left(bucket, sku, key=_sku)]
        if not bucket:
            del self._by_category[product.category]
        return product

    def skus(self) -> list[str]:
        return sorted(self._products)

    def by_category(self, category: str) -> list[Product]:
        return list(self._by_category.get(category, ()))

    def categories(self) -> list[str]:
        return sorted(self._by_category)

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
