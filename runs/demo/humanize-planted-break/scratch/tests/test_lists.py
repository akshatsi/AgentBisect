from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

import humanize


@pytest.mark.parametrize(
    "test_args, expected",
    [
        ([["1", "2", "3"]], "1, 2 and 3"),
        ([["one", "two", "three"]], "one, two and three"),
        ([["one", "two"]], "one and two"),
        ([["one"]], "one"),
        ([[]], ""),
        ([[""]], ""),
        ([[1, 2, 3]], "1, 2 and 3"),
        ([[1, "two"]], "1 and two"),
        ([("one", "two", "three")], "one, two and three"),
        ([("one", "two")], "one and two"),
        ([("one",)], "one"),
        ([{"one": 1, "two": 2}.keys()], "one and two"),
        ([(x for x in ["one", "two", "three"])], "one, two and three"),
        ([range(1, 4)], "1, 2 and 3"),
    ],
)
def test_natural_list(test_args: Iterable[Any], expected: str) -> None:
    assert humanize.natural_list(*test_args) == expected


@pytest.mark.parametrize(
    "items, conjunction, expected",
    [
        (["a", "b", "c"], "or", "a, b or c"),
        (["a", "b"], "or", "a or b"),
        (["a"], "or", "a"),
        ([], "or", ""),
        (["a", "b", "c", "d"], "or", "a, b, c or d"),
        (["a", "b", "c"], "and", "a, b and c"),
    ],
)
def test_natural_list_conjunction(items: Iterable[Any], conjunction: str, expected: str) -> None:
    assert humanize.natural_list(items, conjunction=conjunction) == expected


@pytest.mark.parametrize(
    "items, expected",
    [
        (["a", "b", "c"], "a, b, and c"),
        (["a", "b", "c", "d"], "a, b, c, and d"),
        (["a", "b"], "a and b"),
        (["a"], "a"),
        ([], ""),
        ([1, 2, 3], "1, 2, and 3"),
    ],
)
def test_natural_list_oxford_comma(items: Iterable[Any], expected: str) -> None:
    assert humanize.natural_list(items, oxford_comma=True) == expected


def test_natural_list_conjunction_and_oxford_comma() -> None:
    assert humanize.natural_list(["a", "b", "c"], conjunction="or", oxford_comma=True) == "a, b, or c"
    assert humanize.natural_list(["a", "b"], conjunction="or", oxford_comma=True) == "a or b"


def test_natural_list_defaults_unchanged() -> None:
    assert humanize.natural_list(["a", "b", "c"]) == "a, b and c"
    assert humanize.natural_list(["a", "b"]) == "a and b"
    assert humanize.natural_list(["a"]) == "a"
    assert humanize.natural_list([]) == ""
