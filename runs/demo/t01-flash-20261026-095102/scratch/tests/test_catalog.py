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


def test_search_matches_name_substring(catalog: Catalog):
    assert [p.sku for p in catalog.search("et")] == ["W-1", "W-2"]
    assert catalog.search("zzz") == []


def test_product_validation():
    with pytest.raises(ValueError):
        Product("", "No sku", 100)
    with pytest.raises(ValueError):
        Product("X", "Negative", -1)


def test_update_price_returns_and_stores_the_new_product(catalog: Catalog):
    updated = catalog.update_price("W-1", 450)
    assert updated.sku == "W-1"
    assert updated.price_cents == 450
    assert catalog.get("W-1") == updated
    assert catalog.get("W-1").price_cents == 450


def test_update_price_keeps_other_fields(catalog: Catalog):
    before = catalog.get("E-1")
    after = catalog.update_price("E-1", 1)
    assert after.name == before.name
    assert after.category == before.category
    assert after.taxable == before.taxable
    assert after.weight_g == before.weight_g


def test_update_price_unknown_sku_raises(catalog: Catalog):
    with pytest.raises(UnknownProductError):
        catalog.update_price("NOPE", 100)
    assert len(catalog) == 6


def test_update_price_negative_raises_and_leaves_price_alone(catalog: Catalog):
    with pytest.raises(ValueError):
        catalog.update_price("W-1", -1)
    assert catalog.get("W-1").price_cents == 300


def test_update_price_accepts_zero(catalog: Catalog):
    assert catalog.update_price("F-1", 0).price_cents == 0
    assert catalog.get("F-1").price_cents == 0


def test_iteration_is_sorted_by_sku(catalog: Catalog):
    assert [p.sku for p in catalog] == ["B-1", "E-1", "F-1", "F-2", "W-1", "W-2"]
