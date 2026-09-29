"""Enable only the evidenced-MRP P03 value branch used by ``value_damage``.

P03 stayed value-disabled while the full flow was deferred.  A P03 value leg now
has to name a value-damage origin and equal the bounded portion quantity times
that origin's frozen MRP.  Every other value-bearing posting keeps its unit-cost
truth check.
"""

from importlib import import_module

from django.db import migrations

PREVIOUS = import_module("stockledger.migrations.0013_operational_batch_bounded_value").FORWARD

REPLACEMENTS = (
    (
        "WHERE b.posting_kind IN ('P01','P02','P03','P07','P11','P12','P16','P18')",
        "WHERE b.posting_kind IN ('P01','P02','P07','P11','P12','P16','P18')",
    ),
    (
        """            AND abs(l.amount) = (upper(l.portion) - lower(l.portion)) * o.unit_cost
        ) IS NOT TRUE""",
        """            AND abs(l.amount) = (upper(l.portion) - lower(l.portion))
                * CASE WHEN b.posting_kind = 'P03' THEN o.mrp ELSE o.unit_cost END
            AND (b.posting_kind <> 'P03' OR o.source_kind = 'value_damage')
        ) IS NOT TRUE""",
    ),
)

FORWARD = PREVIOUS
for old, new in REPLACEMENTS:
    if FORWARD.count(old) != 1:  # pragma: no cover - guards the prior function text
        raise RuntimeError("0013's batch function no longer has the expected P03 guard")
    FORWARD = FORWARD.replace(old, new)


class Migration(migrations.Migration):
    dependencies = [("stockledger", "0014_value_damage_basis")]
    operations = [migrations.RunSQL(FORWARD, PREVIOUS)]
