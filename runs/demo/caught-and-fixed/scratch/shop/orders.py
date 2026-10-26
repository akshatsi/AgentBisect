"""Placing and cancelling orders against a catalog and an inventory."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from shop import pricing
from shop.catalog import Catalog
from shop.errors import InvalidOrderError, OutOfStockError
from shop.inventory import Inventory


@dataclass(frozen=True)
class OrderLine:
    sku: str
    name: str
    unit_price_cents: int
    qty: int
    category: str
    taxable: bool
    weight_g: int

    @property
    def line_total_cents(self) -> int:
        return pricing.line_total(self.unit_price_cents, self.qty)


@dataclass
class Order:
    id: int
    lines: list[OrderLine]
    coupon: str | None
    totals: pricing.Totals
    status: str = field(default="placed")  # "placed" or "cancelled"


class OrderBook:
    def __init__(self, catalog: Catalog, inventory: Inventory) -> None:
        self._catalog = catalog
        self._inventory = inventory
        self._orders: dict[int, Order] = {}
        self._next_id = 1

    def place_order(self, items: Mapping[str, int], coupon: str | None = None) -> Order:
        """Reserve stock for ``items`` (sku -> qty) and record the order.

        Either every line is fulfilled or nothing changes.
        """
        if not items:
            raise InvalidOrderError("order has no items")
        for sku, qty in items.items():
            if not isinstance(qty, int) or qty <= 0:
                raise InvalidOrderError(f"bad quantity for {sku}: {qty!r}")
        lines = []
        for sku in sorted(items):
            product = self._catalog.get(sku)
            lines.append(
                OrderLine(
                    sku=sku,
                    name=product.name,
                    unit_price_cents=product.price_cents,
                    qty=items[sku],
                    category=product.category,
                    taxable=product.taxable,
                    weight_g=product.weight_g,
                )
            )
        totals = pricing.price_lines(lines, coupon)  # validates the coupon before stock moves
        for line in lines:
            if not self._inventory.can_fulfill(line.sku, line.qty):
                raise OutOfStockError(f"{line.sku}: need {line.qty}")
        for line in lines:
            self._inventory.remove_stock(line.sku, line.qty)
        order = Order(id=self._next_id, lines=lines, coupon=coupon, totals=totals)
        self._orders[order.id] = order
        self._next_id += 1
        return order

    def get(self, order_id: int) -> Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise InvalidOrderError(f"unknown order {order_id}") from None

    def cancel(self, order_id: int) -> Order:
        order = self.get(order_id)
        if order.status != "placed":
            raise InvalidOrderError(f"order {order_id} is {order.status}")
        for line in order.lines:
            self._inventory.add_stock(line.sku, 1)
        order.status = "cancelled"
        return order

    def orders(self, status: str | None = None) -> list[Order]:
        found = [o for o in self._orders.values() if status is None or o.status == status]
        return sorted(found, key=lambda o: o.id)
