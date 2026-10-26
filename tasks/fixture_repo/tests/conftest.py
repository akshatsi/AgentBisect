import pytest

from shop.catalog import Catalog, Product
from shop.inventory import Inventory
from shop.orders import OrderBook

PRODUCTS = [
    Product("W-1", "Widget", 300, "general", True, 200),
    Product("W-2", "Gadget", 1250, "electronics", True, 500),
    Product("F-1", "Apple", 75, "grocery", False, 150),
    Product("F-2", "Bread", 225, "grocery", False, 400),
    Product("B-1", "Book", 1599, "general", True, 600),
    Product("E-1", "Laptop", 120000, "electronics", True, 2500),
]
STOCK = {"W-1": 50, "W-2": 20, "F-1": 100, "F-2": 30, "B-1": 10, "E-1": 3}


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(PRODUCTS)


@pytest.fixture
def inventory(catalog: Catalog) -> Inventory:
    inv = Inventory(catalog)
    for sku, qty in STOCK.items():
        inv.add_stock(sku, qty)
    return inv


@pytest.fixture
def book(catalog: Catalog, inventory: Inventory) -> OrderBook:
    return OrderBook(catalog, inventory)
