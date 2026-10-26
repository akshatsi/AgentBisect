"""Stock levels per SKU. Every SKU must exist in the catalog."""

from __future__ import annotations

from shop.catalog import Catalog
from shop.errors import OutOfStockError


class Inventory:
    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self._stock: dict[str, int] = {}

    def stock(self, sku: str) -> int:
        self._catalog.get(sku)
        return self._stock.get(sku, 0)

    def add_stock(self, sku: str, qty: int) -> int:
        """Add units and return the new level."""
        self._catalog.get(sku)
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._stock[sku] = self._stock.get(sku, 0) + qty
        return self._stock[sku]

    def remove_stock(self, sku: str, qty: int) -> int:
        """Remove units and return the new level. Raises ``OutOfStockError`` if short."""
        if qty <= 0:
            raise ValueError("qty must be positive")
        have = self.stock(sku)
        if have < qty:
            raise OutOfStockError(f"{sku}: have {have}, need {qty}")
        self._stock[sku] = have - qty
        return self._stock[sku]

    def can_fulfill(self, sku: str, qty: int) -> bool:
        return self.stock(sku) >= qty

    def low_stock(self, threshold: int = 5) -> list[tuple[str, int]]:
        """(sku, qty) for every catalog SKU with at most ``threshold`` units, lowest first."""
        rows = [(sku, self._stock.get(sku, 0)) for sku in self._catalog.skus()]
        return sorted((r for r in rows if r[1] <= threshold), key=lambda r: (r[1], r[0]))

    def total_units(self) -> int:
        return sum(self._stock.values())

    def snapshot(self) -> dict[str, int]:
        """A copy of the stock levels, including zero-stock catalog SKUs."""
        return {sku: self._stock.get(sku, 0) for sku in self._catalog.skus()}
