import pytest

from shop.catalog import Catalog
from shop.errors import OutOfStockError, UnknownProductError
from shop.inventory import Inventory


def test_stock_defaults_to_zero(catalog: Catalog):
    assert Inventory(catalog).stock("W-1") == 0


def test_add_and_remove_stock(inventory: Inventory):
    assert inventory.stock("W-1") == 50
    assert inventory.add_stock("W-1", 5) == 55
    assert inventory.remove_stock("W-1", 15) == 40
    assert inventory.stock("W-1") == 40


def test_remove_more_than_available_raises_and_keeps_stock(inventory: Inventory):
    with pytest.raises(OutOfStockError):
        inventory.remove_stock("E-1", 4)
    assert inventory.stock("E-1") == 3


def test_unknown_sku_raises(inventory: Inventory):
    with pytest.raises(UnknownProductError):
        inventory.stock("NOPE")
    with pytest.raises(UnknownProductError):
        inventory.add_stock("NOPE", 1)


def test_non_positive_quantities_are_rejected(inventory: Inventory):
    with pytest.raises(ValueError):
        inventory.add_stock("W-1", 0)
    with pytest.raises(ValueError):
        inventory.remove_stock("W-1", -2)


def test_can_fulfill(inventory: Inventory):
    assert inventory.can_fulfill("B-1", 10)
    assert not inventory.can_fulfill("B-1", 11)


def test_low_stock_lists_lowest_first(inventory: Inventory):
    assert inventory.low_stock(threshold=10) == [("E-1", 3), ("B-1", 10)]
    inventory.remove_stock("W-2", 20)
    assert inventory.low_stock(threshold=5) == [("W-2", 0), ("E-1", 3)]


def test_totals_and_snapshot(inventory: Inventory):
    assert inventory.total_units() == 213
    snap = inventory.snapshot()
    assert snap["F-1"] == 100
    snap["F-1"] = 0
    assert inventory.stock("F-1") == 100
