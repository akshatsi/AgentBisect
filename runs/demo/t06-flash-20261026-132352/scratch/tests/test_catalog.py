import pytest

from shop.catalog import Catalog, Product
from shop.errors import DuplicateProductError, UnknownProductError


def test_get_returns_the_product(catalog: Catalog):
    assert catalog.get("W-1").name == "Widget"
    assert len(catalog) == 6


def test_get_unknown_raises(catalog: Catalog):
    with pytest.raises(UnknownProductError):
        catalog.get("NOPE")


def test_add_duplicate_raises(catalog: Catalog):
    with pytest.raises(DuplicateProductError):
        catalog.add(Product("W-1", "Other", 100))


def test_remove(catalog: Catalog):
    removed = catalog.remove("F-1")
    assert removed.name == "Apple"
    assert "F-1" not in catalog
    assert len(catalog) == 5


def test_by_category_and_categories(catalog: Catalog):
    assert [p.sku for p in catalog.by_category("grocery")] == ["F-1", "F-2"]
    assert catalog.categories() == ["electronics", "general", "grocery"]


def test_by_category_sees_later_changes(catalog: Catalog):
    catalog.remove("F-2")
    catalog.add(Product("F-3", "Cheese", 650, "grocery", False, 250))
    assert [p.sku for p in catalog.by_category("grocery")] == ["F-1", "F-3"]


def test_category_index_survives_add_remove_and_readd(catalog: Catalog):
    catalog.remove("F-1")
    catalog.remove("F-2")
    assert catalog.by_category("grocery") == []
    assert catalog.categories() == ["electronics", "general"]

    catalog.add(Product("F-2", "Bread", 225, "grocery", False, 400))
    catalog.add(Product("F-1", "Apple", 75, "grocery", False, 150))
    assert [p.sku for p in catalog.by_category("grocery")] == ["F-1", "F-2"]
    assert catalog.categories() == ["electronics", "general", "grocery"]

    catalog.remove("F-1")
    catalog.add(Product("F-1", "Apple", 75, "grocery", False, 150))
    assert [p.sku for p in catalog.by_category("grocery")] == ["F-1", "F-2"]

    catalog.remove("F-2")
    assert [p.sku for p in catalog.by_category("grocery")] == ["F-1"]
    assert catalog.categories() == ["electronics", "general", "grocery"]

    # The returned list is a copy, so callers cannot corrupt the index.
    catalog.by_category("grocery").clear()
    assert [p.sku for p in catalog.by_category("grocery")] == ["F-1"]
    assert catalog.categories() == ["electronics", "general", "grocery"]


def test_search_matches_name_substring(catalog: Catalog):
    assert [p.sku for p in catalog.search("et")] == ["W-1", "W-2"]
    assert catalog.search("zzz") == []


def test_product_validation():
    with pytest.raises(ValueError):
        Product("", "No sku", 100)
    with pytest.raises(ValueError):
        Product("X", "Negative", -1)


def test_iteration_is_sorted_by_sku(catalog: Catalog):
    assert [p.sku for p in catalog] == ["B-1", "E-1", "F-1", "F-2", "W-1", "W-2"]
