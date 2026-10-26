import pytest

from shop.money import format_cents, round_half_up, split_evenly, to_cents


def test_to_cents_parses_strings_and_floats():
    assert to_cents("12.34") == 1234
    assert to_cents(5) == 500
    assert to_cents(0.1) == 10


def test_to_cents_rounds_half_up():
    assert to_cents("1.005") == 101
    assert to_cents("1.004") == 100


def test_round_half_up_goes_away_from_zero_on_ties():
    assert round_half_up(2.5) == 3
    assert round_half_up(3.5) == 4
    assert round_half_up(2.4) == 2


def test_format_cents():
    assert format_cents(1234) == "$12.34"
    assert format_cents(5) == "$0.05"
    assert format_cents(0) == "$0.00"


def test_format_cents_negative_and_symbol():
    assert format_cents(-150) == "-$1.50"
    assert format_cents(250, symbol="EUR ") == "EUR 2.50"
    assert format_cents(250, symbol="") == "2.50"


def test_split_evenly_spreads_the_remainder():
    assert split_evenly(10, 3) == [4, 3, 3]
    assert sum(split_evenly(1001, 7)) == 1001


def test_split_evenly_rejects_zero_parts():
    with pytest.raises(ValueError):
        split_evenly(100, 0)
