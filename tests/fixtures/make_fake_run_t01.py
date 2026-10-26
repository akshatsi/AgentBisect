"""Regenerate ``fake_run_t01.jsonl``: a scripted ~14-step agent run on tasks/t01.yaml.

    uv run python tests/fixtures/make_fake_run_t01.py

The story: the agent adds ``Catalog.update_price`` correctly and its own tests pass, then (step 8)
makes ``Catalog.add`` silently replace existing SKUs, which breaks the old test
``test_add_duplicate_raises``. It only runs the full suite at step 11, tries a wrong fix, and
finishes with the test still red. So t_err = 8, t_obs = 11, lag = 3.
"""

from pathlib import Path

from agentbisect.llm import ChatResponse, ToolCall, dump_responses
from agentbisect.models import Usage

OUT = Path(__file__).with_name("fake_run_t01.jsonl")

TESTS = """import pytest

from shop.errors import UnknownProductError


def test_update_price_returns_updated_product(catalog):
    updated = catalog.update_price("W-1", 350)
    assert updated.price_cents == 350
    assert catalog.get("W-1") is updated


def test_update_price_unknown_sku(catalog):
    with pytest.raises(UnknownProductError):
        catalog.update_price("NOPE", 100)


def test_update_price_rejects_negative(catalog):
    with pytest.raises(ValueError):
        catalog.update_price("W-1", -1)
    assert catalog.get("W-1").price_cents == 300


def test_old_orders_keep_their_price(catalog, book):
    first = book.place_order({"W-1": 2})
    catalog.update_price("W-1", 400)
    second = book.place_order({"W-1": 2})
    assert first.lines[0].unit_price_cents == 300
    assert second.lines[0].unit_price_cents == 400
"""

UPDATE_PRICE = '''    def update_price(self, sku: str, price_cents: int) -> Product:
        """Set a product's price and return the updated product.

        Orders copy the price into their lines, so earlier orders keep the price they had.
        """
        product = self.get(sku)
        if price_cents < 0:
            raise ValueError("price must not be negative")
        updated = replace(product, price_cents=price_cents)
        self._products[sku] = updated
        return updated

    def skus(self) -> list[str]:'''

# (reasoning, tool, args, tokens_in, tokens_out)
SCRIPT = [
    (
        "I will start by reading the catalog module.",
        "read_file",
        {"path": "shop/catalog.py"},
        900,
        40,
    ),
    (
        "Orders matter for the price rule, so I will read how they copy prices.",
        "read_file",
        {"path": "shop/orders.py"},
        2100,
        40,
    ),
    (
        "Let me run the existing tests to get a baseline.",
        "run_shell",
        {"command": "python -m pytest -q"},
        3300,
        35,
    ),
    (
        "I need `replace` to copy a frozen dataclass with a new price.",
        "edit_file",
        {
            "path": "shop/catalog.py",
            "old": "from dataclasses import dataclass",
            "new": "from dataclasses import dataclass, replace",
        },
        3500,
        60,
    ),
    (
        "Now I add update_price before `skus`.",
        "edit_file",
        {"path": "shop/catalog.py", "old": "    def skus(self) -> list[str]:", "new": UPDATE_PRICE},
        3800,
        260,
    ),
    (
        "I will write tests for the new method, including the order price rule.",
        "write_file",
        {"path": "tests/test_catalog_update.py", "content": TESTS},
        4300,
        330,
    ),
    (
        "Run the new tests.",
        "run_shell",
        {"command": "python -m pytest tests/test_catalog_update.py -q"},
        4900,
        40,
    ),
    (
        "While I am here, I will make `add` replace an existing SKU instead of raising, so one method covers both cases.",
        "edit_file",
        {
            "path": "shop/catalog.py",
            "old": "        if product.sku in self._products:\n            raise DuplicateProductError(product.sku)\n        self._products[product.sku] = product",
            "new": "        self._products[product.sku] = product",
        },
        5200,
        120,
    ),
    (
        "Document the new method in the README.",
        "edit_file",
        {
            "path": "README.md",
            "old": "- `shop/catalog.py`   `Product` and `Catalog`",
            "new": "- `shop/catalog.py`   `Product` and `Catalog` (`Catalog.update_price` changes a price)",
        },
        5500,
        90,
    ),
    (
        "Check that orders still work.",
        "run_shell",
        {"command": "python -m pytest tests/test_orders.py -q"},
        5700,
        35,
    ),
    (
        "Now the whole suite, to confirm nothing else broke.",
        "run_shell",
        {"command": "python -m pytest -q"},
        5900,
        35,
    ),
    (
        "One old test fails. I think `update_price` needs a second look at the validation order.",
        "edit_file",
        {
            "path": "shop/catalog.py",
            "old": '        product = self.get(sku)\n        if price_cents < 0:\n            raise ValueError("price must not be negative")',
            "new": '        if price_cents < 0:\n            raise ValueError("price must not be negative")\n        product = self.get(sku)',
        },
        6600,
        110,
    ),
    ("Run the suite again.", "run_shell", {"command": "python -m pytest -q"}, 6900, 35),
    (
        "The remaining failure looks unrelated to my change, so I will stop here.",
        "finish",
        {
            "summary": "Added Catalog.update_price with tests. One older catalog test still fails; I could not find the cause."
        },
        7300,
        70,
    ),
]


def build() -> list[ChatResponse]:
    out = []
    for i, (reason, tool, args, tin, tout) in enumerate(SCRIPT, 1):
        out.append(
            ChatResponse(
                content=reason,
                tool_calls=[ToolCall(id=f"call_{i}", name=tool, arguments=args)],
                usage=Usage(tokens_in=tin, tokens_out=tout),
            )
        )
    return out


if __name__ == "__main__":
    dump_responses(OUT, build())
    print(f"wrote {OUT}")
