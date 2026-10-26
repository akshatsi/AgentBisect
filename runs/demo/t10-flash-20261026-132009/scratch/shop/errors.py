"""Exception types shared by every module."""


class ShopError(Exception):
    """Base class for all errors raised by this package."""


class UnknownProductError(ShopError):
    """A SKU is not in the catalog."""


class DuplicateProductError(ShopError):
    """A SKU is already in the catalog."""


class OutOfStockError(ShopError):
    """Not enough units in stock."""


class InvalidOrderError(ShopError):
    """The order is empty, malformed, or in the wrong state for the request."""


class InvalidCouponError(ShopError):
    """The coupon code is not known."""
