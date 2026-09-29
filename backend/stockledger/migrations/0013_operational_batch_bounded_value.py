"""Close two gaps in 0012's true-value check (fix ticket 08 review).

* An open-ended portion such as ``[0,)`` made the amount comparison NULL, so the leg was
  never flagged. The whole condition must now be true: a named lot, a bounded non-empty
  portion, an existing origin of the tenant and an amount of pieces x unit cost.
* P03 may carry no value while ``value_damage`` stays refused (change PRD §14.3). Its
  value basis is the MRP frozen on the decision, not an origin's, and arrives with that
  flow; until then a P03 value leg is refused rather than checked against the wrong basis.
"""

from importlib import import_module

from django.db import migrations

PREVIOUS = import_module("stockledger.migrations.0012_operational_batch_value_truth").FORWARD

REPLACEMENTS = (
    (
        "WHERE b.posting_kind IN ('P01','P02','P07','P11','P12','P16','P18')",
        "WHERE b.posting_kind IN ('P01','P02','P03','P07','P11','P12','P16','P18')",
    ),
    (
        """        WHERE o.id IS NULL OR l.lot_id IS NULL OR l.portion IS NULL OR isempty(l.portion)
            OR abs(l.amount) <> (upper(l.portion) - lower(l.portion))
                * CASE WHEN b.posting_kind = 'P03' THEN o.mrp ELSE o.unit_cost END""",
        """        WHERE (
            o.id IS NOT NULL AND l.lot_id IS NOT NULL AND l.portion IS NOT NULL
            AND NOT isempty(l.portion) AND NOT lower_inf(l.portion) AND NOT upper_inf(l.portion)
            AND abs(l.amount) = (upper(l.portion) - lower(l.portion)) * o.unit_cost
        ) IS NOT TRUE""",
    ),
)

FORWARD = PREVIOUS
for old, new in REPLACEMENTS:
    if FORWARD.count(old) != 1:  # pragma: no cover - guards the text this builds on
        raise RuntimeError("0012's batch function no longer has the check this replaces")
    FORWARD = FORWARD.replace(old, new)


class Migration(migrations.Migration):
    dependencies = [("stockledger", "0012_operational_batch_value_truth")]
    operations = [migrations.RunSQL(FORWARD, PREVIOUS)]
