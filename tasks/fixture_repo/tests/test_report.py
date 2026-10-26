import csv
import io

from shop.catalog import Catalog
from shop.inventory import Inventory
from shop.orders import OrderBook
from shop.report import (
    inventory_report,
    order_summary,
    orders_to_csv,
    sales_by_category,
    top_products,
)


def test_order_summary_text(book: OrderBook):
    order = book.place_order({"W-1": 2, "F-1": 4}, coupon="SAVE10")
    assert order_summary(order).splitlines() == [
        "Order #1 [placed]",
        "  4 x Apple @ $0.75 = $3.00",
        "  2 x Widget @ $3.00 = $6.00",
        "  Subtotal: $9.00",
        "  Discount: -$0.90",
        "  Tax: $0.48",
        "  Shipping: $6.00",
        "  Total: $14.58",
    ]


def test_order_summary_omits_discount_line_without_coupon(book: OrderBook):
    order = book.place_order({"W-1": 1})
    assert "Discount" not in order_summary(order)


def test_sales_by_category(book: OrderBook):
    orders = [
        book.place_order({"W-1": 2, "F-1": 4}),
        book.place_order({"W-2": 1, "W-1": 5}),
    ]
    assert sales_by_category(orders) == {
        "electronics": 1250,
        "general": 600 + 1425,
        "grocery": 300,
    }


def test_top_products_orders_by_units_then_sku(book: OrderBook):
    orders = [
        book.place_order({"W-1": 5, "F-1": 5, "B-1": 1}),
        book.place_order({"W-1": 1, "F-2": 2}),
    ]
    assert top_products(orders, n=3) == [("W-1", 6), ("F-1", 5), ("F-2", 2)]


def test_top_products_ignores_cancelled_orders(book: OrderBook):
    keep = book.place_order({"F-1": 3})
    drop = book.place_order({"W-1": 9})
    book.cancel(drop.id)
    assert top_products([keep, drop]) == [("F-1", 3)]


def test_inventory_report(inventory: Inventory, catalog: Catalog):
    inventory.remove_stock("B-1", 4)
    lines = inventory_report(inventory, catalog).splitlines()
    assert lines[0] == "SKU    Name        Qty"
    assert lines[1] == "B-1    Book          6"
    assert len(lines) == 7


def test_orders_to_csv_uses_plain_decimals(book: OrderBook):
    order = book.place_order({"E-1": 1, "W-1": 5})
    rows = list(csv.reader(io.StringIO(orders_to_csv([order]))))
    assert rows[0] == ["order_id", "status", "sku", "qty", "unit_price", "line_total"]
    assert rows[1] == ["1", "placed", "E-1", "1", "1200.00", "1200.00"]
    assert rows[2] == ["1", "placed", "W-1", "5", "3.00", "14.25"]
