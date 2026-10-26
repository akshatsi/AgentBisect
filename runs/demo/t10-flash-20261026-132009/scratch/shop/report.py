"""Text and CSV reports."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from shop.catalog import Catalog
from shop.inventory import Inventory
from shop.money import format_cents
from shop.orders import Order


def order_summary(order: Order) -> str:
    lines = [f"Order #{order.id} [{order.status}]"]
    for line in order.lines:
        lines.append(
            f"  {line.qty} x {line.name} @ {format_cents(line.unit_price_cents)}"
            f" = {format_cents(line.line_total_cents)}"
        )
    t = order.totals
    lines.append(f"  Subtotal: {format_cents(t.subtotal_cents)}")
    if t.discount_cents:
        lines.append(f"  Discount: -{format_cents(t.discount_cents)}")
    lines.append(f"  Tax: {format_cents(t.tax_cents)}")
    lines.append(f"  Shipping: {format_cents(t.shipping_cents)}")
    lines.append(f"  Total: {format_cents(t.total_cents)}")
    return "\n".join(lines)


def sales_by_category(orders: Iterable[Order]) -> dict[str, int]:
    """Line totals in cents per category, summed over ``orders``, ignoring cancelled orders."""
    sales: dict[str, int] = {}
    for order in orders:
        if order.status == "cancelled":
            continue
        for line in order.lines:
            sales[line.category] = sales.get(line.category, 0) + line.line_total_cents
    return dict(sorted(sales.items()))


def top_products(orders: Iterable[Order], n: int = 3) -> list[tuple[str, int]]:
    """The ``n`` best-selling SKUs by units, ignoring cancelled orders."""
    units: dict[str, int] = {}
    for order in orders:
        if order.status == "cancelled":
            continue
        for line in order.lines:
            units[line.sku] = units.get(line.sku, 0) + line.qty
    ranked = sorted(units.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:n]


def inventory_report(inventory: Inventory, catalog: Catalog) -> str:
    rows = ["SKU    Name        Qty"]
    for product in catalog:
        rows.append(f"{product.sku:<6} {product.name:<11} {inventory.stock(product.sku):>3}")
    return "\n".join(rows)


def orders_to_csv(orders: Iterable[Order]) -> str:
    """One row per order line. Amounts are plain decimals (no symbol) so tools can parse them."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["order_id", "status", "sku", "qty", "unit_price", "line_total"])
    for order in orders:
        for line in order.lines:
            writer.writerow(
                [
                    order.id,
                    order.status,
                    line.sku,
                    line.qty,
                    format_cents(line.unit_price_cents, symbol=""),
                    format_cents(line.line_total_cents, symbol=""),
                ]
            )
    return out.getvalue()
