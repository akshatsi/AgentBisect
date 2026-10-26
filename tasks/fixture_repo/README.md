# shop

A small library for a catalog, stock levels and orders. Money is always integer cents.

Modules:

- `shop/errors.py`    shared exception types
- `shop/money.py`     cents parsing, rounding and formatting
- `shop/catalog.py`   `Product` and `Catalog`
- `shop/inventory.py` stock levels per SKU
- `shop/pricing.py`   bulk discounts, coupons, tax and shipping
- `shop/orders.py`    `OrderBook`: place and cancel orders
- `shop/report.py`    text and CSV reports

Run the tests with `python -m pytest`.
