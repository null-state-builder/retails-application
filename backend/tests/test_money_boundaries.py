"""Exact money boundaries used by imports, documents and the offline till."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.goods_money import MoneyInvalid, paise_from_amount, paise_from_json
from core.money import MoneyField, paise_to_rupees_str, rupees_to_paise
from sell.refund_math import refund_share


def test_json_and_spreadsheet_amounts_keep_unknown_distinct_from_zero() -> None:
    assert paise_from_json(None) is None
    assert paise_from_json("0") == 0
    assert paise_from_amount("") is None
    assert paise_from_amount(Decimal("0.00")) == 0
    assert paise_from_amount(Decimal("100.61")) == 10_061


@pytest.mark.parametrize("value", [1, 1.0, True, "1.2", "-1", "NaN", ""])
def test_json_money_refuses_non_integer_paise_strings(value: object) -> None:
    with pytest.raises(MoneyInvalid):
        paise_from_json(value)


@pytest.mark.parametrize("value", [1.25, True, Decimal("1.001"), "Infinity"])
def test_spreadsheet_money_refuses_lossy_or_invalid_input(value: object) -> None:
    with pytest.raises(MoneyInvalid):
        paise_from_amount(value)


def test_money_field_refuses_coercion_after_input_boundary() -> None:
    field: MoneyField[int, int] = MoneyField()
    assert field.get_prep_value(101) == 101
    for value in (True, 1.9, Decimal("1"), "101"):
        with pytest.raises(TypeError):
            field.get_prep_value(value)


def test_rupee_rounding_and_rendering_are_exact() -> None:
    assert rupees_to_paise(Decimal("10.005")) == 1001
    assert paise_to_rupees_str(-1001) == "-10.01"
    with pytest.raises(TypeError):
        rupees_to_paise(1.0)  # type: ignore[arg-type]  # intentional runtime refusal


def test_partial_refunds_round_half_up_and_final_piece_settles_remainder() -> None:
    first = refund_share(
        paid_paise=1000, line_qty=3, returning=1, returned_qty=0, returned_paise=0
    )
    second = refund_share(
        paid_paise=1000, line_qty=3, returning=1, returned_qty=1, returned_paise=first
    )
    third = refund_share(
        paid_paise=1000, line_qty=3, returning=1, returned_qty=2, returned_paise=first + second
    )
    assert (first, second, third) == (333, 333, 334)

    # A partial half-paise rounds up, as the offline till's reference cases do.
    assert refund_share(
        paid_paise=1005, line_qty=2, returning=1, returned_qty=0, returned_paise=0
    ) == 503
