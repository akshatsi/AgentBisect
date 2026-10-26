from dataclasses import dataclass
from decimal import Decimal

import pytest

from shop import pricing
from shop.errors import InvalidCouponError


@dataclass
class Line:
    unit_price_cents: int
    qty: int
    category: str = "general"
    taxable: bool = True
    weight_g: int = 0


def test_bulk_discount_tiers():
    assert pricing.bulk_discount_percent(1) == 0
    assert pricing.bulk_discount_percent(4) == 0
    assert pricing.bulk_discount_percent(5) == 5
    assert pricing.bulk_discount_percent(9) == 5
    assert pricing.bulk_discount_percent(10) == 10
    assert pricing.bulk_discount_percent(50) == 10


def test_apply_percent_rounds_half_up():
    assert pricing.apply_percent(1000, 10) == 100
    assert pricing.apply_percent(5, 50) == 3
    assert pricing.apply_percent(0, 10) == 0


def test_line_total_applies_bulk_discount():
    assert pricing.line_total(300, 4) == 1200
    assert pricing.line_total(300, 5) == 1425
    assert pricing.line_total(300, 10) == 2700


def test_coupon_discount_percent_and_fixed():
    assert pricing.coupon_discount(4000, "SAVE10") == 400
    assert pricing.coupon_discount(4000, "FIVEOFF") == 500
    assert pricing.coupon_discount(4000, None) == 0


def test_fixed_coupon_never_exceeds_subtotal():
    assert pricing.coupon_discount(300, "FIVEOFF") == 300


def test_unknown_coupon_raises():
    with pytest.raises(InvalidCouponError):
        pricing.coupon_discount(1000, "BOGUS")


def test_tax_rate_by_category():
    assert pricing.tax_rate("electronics") == Decimal("0.10")
    assert pricing.tax_rate("grocery") == Decimal("0")
    assert pricing.tax_rate("unheard-of") == Decimal("0.08")


def test_shipping_rules():
    assert pricing.shipping_cents(0, 0) == 0
    assert pricing.shipping_cents(1000, 500) == 500
    assert pricing.shipping_cents(1000, 2500) == 700
    assert pricing.shipping_cents(5000, 9000) == 0
    assert pricing.shipping_cents(4999, 0) == 500


def test_price_lines_with_coupon_tax_and_shipping():
    lines = [
        Line(1000, 2, "general", True, 500),
        Line(200, 3, "grocery", False, 100),
    ]
    totals = pricing.price_lines(lines, "SAVE10")
    assert totals.subtotal_cents == 2600
    assert totals.discount_cents == 260
    assert totals.tax_cents == 160
    assert totals.shipping_cents == 600
    assert totals.total_cents == 2600 - 260 + 160 + 600


def test_price_lines_free_shipping_after_coupon_is_checked():
    lines = [Line(5200, 1)]
    assert pricing.price_lines(lines).shipping_cents == 0
    assert pricing.price_lines(lines, "FIVEOFF").shipping_cents == 500
