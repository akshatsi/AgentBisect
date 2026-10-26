import pytest

from shop.catalog import Catalog
from shop.errors import (
    InvalidCouponError,
    InvalidOrderError,
    OutOfStockError,
    UnknownProductError,
)
from shop.inventory import Inventory
from shop.orders import OrderBook


def test_place_order_prices_lines_and_reduces_stock(book: OrderBook, inventory: Inventory):
    order = book.place_order({"W-1": 2, "B-1": 1})
    assert order.id == 1
    assert order.status == "placed"
    assert [line.sku for line in order.lines] == ["B-1", "W-1"]
    assert order.totals.subtotal_cents == 1599 + 600
    assert order.totals.tax_cents == 176
    assert order.totals.shipping_cents == 600
    assert order.totals.total_cents == 2199 + 176 + 600
    assert inventory.stock("W-1") == 48
    assert inventory.stock("B-1") == 9


def test_order_ids_increase(book: OrderBook):
    assert book.place_order({"W-1": 1}).id == 1
    assert book.place_order({"W-1": 1}).id == 2


def test_order_with_coupon(book: OrderBook):
    order = book.place_order({"W-2": 4}, coupon="SAVE10")
    assert order.totals.subtotal_cents == 5000
    assert order.totals.discount_cents == 500
    assert order.totals.shipping_cents == 700


def test_out_of_stock_changes_nothing(book: OrderBook, inventory: Inventory):
    with pytest.raises(OutOfStockError):
        book.place_order({"B-1": 2, "W-1": 60})
    assert inventory.stock("B-1") == 10
    assert inventory.stock("W-1") == 50
    assert book.orders() == []


def test_unknown_sku_and_bad_coupon_change_nothing(book: OrderBook, inventory: Inventory):
    with pytest.raises(UnknownProductError):
        book.place_order({"NOPE": 1})
    with pytest.raises(InvalidCouponError):
        book.place_order({"W-1": 1}, coupon="BOGUS")
    assert inventory.stock("W-1") == 50
    assert book.orders() == []


def test_invalid_orders_are_rejected(book: OrderBook):
    with pytest.raises(InvalidOrderError):
        book.place_order({})
    with pytest.raises(InvalidOrderError):
        book.place_order({"W-1": 0})
    with pytest.raises(InvalidOrderError):
        book.place_order({"W-1": 1.5})  # type: ignore[dict-item]


def test_cancel_restores_stock(book: OrderBook, inventory: Inventory):
    order = book.place_order({"F-1": 10, "W-1": 3})
    assert inventory.stock("F-1") == 90
    cancelled = book.cancel(order.id)
    assert cancelled.status == "cancelled"
    assert inventory.stock("F-1") == 100
    assert inventory.stock("W-1") == 50


def test_cancel_twice_or_unknown_raises(book: OrderBook):
    order = book.place_order({"W-1": 1})
    book.cancel(order.id)
    with pytest.raises(InvalidOrderError):
        book.cancel(order.id)
    with pytest.raises(InvalidOrderError):
        book.cancel(99)


def test_orders_filter_by_status(book: OrderBook):
    a = book.place_order({"W-1": 1})
    b = book.place_order({"W-1": 2})
    book.cancel(a.id)
    assert [o.id for o in book.orders()] == [1, 2]
    assert [o.id for o in book.orders("placed")] == [b.id]
    assert [o.id for o in book.orders("cancelled")] == [a.id]


def test_order_keeps_the_price_it_was_placed_at(book: OrderBook):
    order = book.place_order({"W-1": 1})
    assert order.lines[0].unit_price_cents == 300
    assert book.get(order.id) is order


def test_price_change_leaves_placed_orders_unchanged(book: OrderBook, catalog: Catalog):
    order = book.place_order({"W-1": 1})
    subtotal_before = order.totals.subtotal_cents
    total_before = order.totals.total_cents

    catalog.update_price("W-1", 400)

    assert book.get(order.id).lines[0].unit_price_cents == 300
    assert book.get(order.id).totals.subtotal_cents == subtotal_before
    assert book.get(order.id).totals.total_cents == total_before


def test_new_orders_use_the_new_price(book: OrderBook, catalog: Catalog):
    catalog.update_price("W-1", 400)
    order = book.place_order({"W-1": 2})
    assert order.lines[0].unit_price_cents == 400
    assert order.totals.subtotal_cents == 800


def test_price_change_between_orders(book: OrderBook, catalog: Catalog):
    old = book.place_order({"W-1": 3})
    catalog.update_price("W-1", 100)
    new = book.place_order({"W-1": 3})
    assert old.lines[0].unit_price_cents == 300
    assert new.lines[0].unit_price_cents == 100
    assert old.totals.subtotal_cents == 900
    assert new.totals.subtotal_cents == 300
