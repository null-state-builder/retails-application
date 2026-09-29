"""Generated data for rehearsing the salesperson move (ticket 07, baseline B7).

Writes, at the schema *before* the move (sell migration 0026), what a year of
KDPS selling leaves behind: every store's old salesperson rows, a year of posted
bills, one or two lines each, every sold line naming who sold it, and each
bill's recorded default seller. Alongside, staff records on the staff list that
the matching rule will and will not be able to place:

* seven in ten old rows have a staff record at their store with the same name;
* one in ten has one with the same code;
* two in ten have none (left for Admin);
* one store has two staff records sharing a name (left for Admin: no guessing).

Plain SQL with ``generate_series``, because the point is volume. Used by
``manage.py rehearse_salesperson_move`` on a throwaway database and, at a small
size, by the migration test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SALES_SQL = """
    INSERT INTO sell_sale (
        doc_number, idempotency_uuid, docstatus, created_at, updated_at, fy, till_seq,
        origin, billed_at, customer_name, customer_mobile, buyer_gstin, b2b_tax_kind,
        gross_paise, discount_paise, net_paise, gst_paise, round_paise, created_by_id,
        store_id, override_kind, payload_fingerprint, till_number, tax_setting_version
    )
    SELECT 'RH/' || %(store)s || '/' || g, gen_random_uuid(), 0, now(), now(), '25-26', g,
           'online', now() - ((g %% 365) * interval '1 day'), '', '', '', '',
           50000, 0, 50000, 2381, 0, %(user)s,
           %(store)s, '', '', '', 1
    FROM generate_series(1, %(bills)s) g
"""

LINES_SQL = """
    INSERT INTO sell_sale_line (
        created_at, updated_at, line_no, direction, barcode, season, design, color, size,
        brand, item, hsn, qty, mrp_paise, disc_paise, net_paise, gst_rate, gst_paise,
        unit_cost_paise, offer_evidence, manual_desc, sold_before_inward, costing_status,
        return_reason, condition, sale_id, salesman_id, cost_book, goods_allocations
    )
    SELECT now(), now(), ln, 'sale', '890' || s.id, '', '', '', '', 'RH', '', '6205', 1,
           50000, 0, 50000, 5, 2381, 0, '{}'::jsonb, '', false, 'posted', '', '', s.id,
           (%(sellers)s::bigint[])[1 + ((s.till_seq + ln) %% %(count)s)], '', '[]'::jsonb
    FROM sell_sale s CROSS JOIN generate_series(1, 2) ln
    WHERE s.store_id = %(store)s AND s.doc_number LIKE 'RH/%%'
      AND (ln = 1 OR s.till_seq %% 2 = 0)
"""

DEFAULTS_SQL = """
    WITH firsts AS (
        SELECT DISTINCT ON (sale_id) sale_id, salesman_id
        FROM sell_sale_line WHERE salesman_id IS NOT NULL ORDER BY sale_id, id
    )
    UPDATE sell_sale s SET salesman_default_id = f.salesman_id
    FROM firsts f
    WHERE f.sale_id = s.id AND s.docstatus = 0 AND s.doc_number LIKE 'RH/%%'
"""

POST_SQL = "UPDATE sell_sale SET docstatus = 1 WHERE docstatus = 0 AND doc_number LIKE 'RH/%%'"


@dataclass(frozen=True)
class Planned:
    """One old salesperson row, and the staff record (if any) it should meet."""

    store_id: int
    code: str
    name: str
    staff_code: str | None
    staff_name: str | None


def plan_sellers(stores: list[tuple[int, str]], per_store: int) -> list[Planned]:
    planned: list[Planned] = []
    for index, (store_id, store_code) in enumerate(stores):
        for g in range(1, per_store + 1):
            code = f"{store_code}-R{g}"[:16]
            name = f"Seller {g} of {store_code}"
            kind = g % 10
            if kind < 7:
                planned.append(Planned(store_id, code, name, f"RH-{store_code}-{g}", name.upper()))
            elif kind == 7:
                planned.append(Planned(store_id, code, name, code, f"Someone {g} {store_code}"))
            else:
                planned.append(Planned(store_id, code, name, None, None))
        if index == 0:
            # Two people at one store with one name: the rule must not pick.
            twin = f"Twin Name {store_code}"
            planned.append(
                Planned(store_id, f"{store_code}-TW"[:16], twin, f"RH-{store_code}-T1", twin)
            )
            planned.append(
                Planned(store_id, f"{store_code}-TX"[:16], "Other", f"RH-{store_code}-T2", twin)
            )
    return planned


def write_old_rows(cursor: Any, planned: list[Planned]) -> dict[int, list[int]]:
    """The old salesperson rows; returns each store's row ids."""
    by_store: dict[int, list[int]] = {}
    for row in planned:
        cursor.execute(
            "INSERT INTO sell_salesman (created_at, updated_at, code, name, is_active, store_id) "
            "VALUES (now(), now(), %s, %s, true, %s) RETURNING id",
            [row.code, row.name, row.store_id],
        )
        by_store.setdefault(row.store_id, []).append(int(cursor.fetchone()[0]))
    return by_store


def write_bills(
    cursor: Any,
    sellers: dict[int, list[int]],
    *,
    bills_per_store: int,
    user_id: int,
    post: bool = True,
) -> None:
    """A year of bills; ``post=False`` leaves them drafts (a test bends one first)."""
    for store_id, ids in sellers.items():
        cursor.execute(SALES_SQL, {"store": store_id, "user": user_id, "bills": bills_per_store})
        cursor.execute(LINES_SQL, {"store": store_id, "sellers": ids, "count": len(ids)})
    # Fresh tables have no statistics, and without them the planner joins a
    # year of bills to their lines row by row.
    cursor.execute("ANALYZE sell_sale")
    cursor.execute("ANALYZE sell_sale_line")
    cursor.execute(DEFAULTS_SQL)
    if post:
        cursor.execute(POST_SQL)
    cursor.execute("ANALYZE sell_sale")
    cursor.execute("ANALYZE sell_sale_line")
