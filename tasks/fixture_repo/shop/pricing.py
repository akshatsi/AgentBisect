"""Bulk discounts, coupons, tax and shipping. All amounts are integer cents."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from shop.errors import InvalidCouponError
from shop.money import round_half_up

# (minimum quantity, percent off the line), checked from the largest tier down.
BULK_TIERS: tuple[tuple[int, int], ...] = ((10, 10), (5, 5))

# code -> (kind, value). "percent" is a whole percent, "fixed" is cents.
COUPONS: dict[str, tuple[str, int]] = {
    "SAVE10": ("percent", 10),
    "FIVEOFF": ("fixed", 500),
}

TAX_RATES: dict[str, Decimal] = {
    "general": Decimal("0.08"),
    "electronics": Decimal("0.10"),
    "grocery": Decimal("0"),
}
DEFAULT_TAX_RATE = Decimal("0.08")

FREE_SHIPPING_AT_CENTS = 5000
BASE_SHIPPING_CENTS = 500
PER_KG_SHIPPING_CENTS = 100


class PricedLine(Protocol):
    unit_price_cents: int
    qty: int
    category: str
    taxable: bool
    weight_g: int


@dataclass(frozen=True)
class Totals:
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    shipping_cents: int
    total_cents: int


def bulk_discount_percent(qty: int) -> int:
    for min_qty, percent in BULK_TIERS:
        if qty >= min_qty:
            return percent
    return 0


def apply_percent(cents: int, percent: int) -> int:
    """The amount that ``percent`` percent of ``cents`` comes to, rounded half up."""
    return round_half_up(Decimal(cents) * Decimal(percent) / 100)


def line_total(unit_price_cents: int, qty: int) -> int:
    """Price of ``qty`` units after the bulk discount."""
    gross = unit_price_cents * qty
    return gross - apply_percent(gross, bulk_discount_percent(qty))


def coupon_discount(subtotal_cents: int, code: str | None) -> int:
    """Cents off for ``code``. Never more than the subtotal."""
    if code is None:
        return 0
    if code not in COUPONS:
        raise InvalidCouponError(code)
    kind, value = COUPONS[code]
    off = apply_percent(subtotal_cents, value) if kind == "percent" else value
    return min(off, subtotal_cents)


def tax_rate(category: str) -> Decimal:
    return TAX_RATES.get(category, DEFAULT_TAX_RATE)


def shipping_cents(subtotal_cents: int, weight_g: int) -> int:
    if subtotal_cents <= 0:
        return 0
    if subtotal_cents >= FREE_SHIPPING_AT_CENTS:
        return 0
    return BASE_SHIPPING_CENTS + (weight_g // 1000) * PER_KG_SHIPPING_CENTS


def price_lines(lines: Iterable[PricedLine], coupon: str | None = None) -> Totals:
    """Price an order. Tax is charged on discounted line totals, before any coupon."""
    lines = list(lines)
    subtotal = 0
    weight = 0
    taxable_by_category: dict[str, int] = {}
    for line in lines:
        total = line_total(line.unit_price_cents, line.qty)
        subtotal += total
        weight += line.weight_g * line.qty
        if line.taxable:
            taxable_by_category[line.category] = taxable_by_category.get(line.category, 0) + total
    discount = coupon_discount(subtotal, coupon)
    tax = sum(
        round_half_up(Decimal(base) * tax_rate(category))
        for category, base in taxable_by_category.items()
    )
    shipping = shipping_cents(subtotal - discount, weight)
    return Totals(
        subtotal_cents=subtotal,
        discount_cents=discount,
        tax_cents=tax,
        shipping_cents=shipping,
        total_cents=subtotal - discount + tax + shipping,
    )
