"""The ledger learns the store sale's own posting kind, P19 (OPS-07).

The database is the last word on what a posting may be: the catalogue of kinds is
written into `kdps_append_operational_batch`, so a kind Python alone knew about
would be refused at the moment a shop tried to bill.

Selling a piece at a goods-v1 store is stock leaving to a customer. It is not
P13's adjustment-down, write-off or shrinkage, and recording it as one would make
every shop's takings read as losses in the one record meant to say what happened.
So it is a kind of its own, and this migration adds it to the list - and to
nothing else. P19 carries value (it is a piece leaving at its origin's own cost),
so it stays out of the value-less list 0012 added, and the amount check there
applies to it unchanged: a sale's value leg is its pieces times that origin's
frozen unit cost, which is exactly what the sale service posts.

Built from 0015's text rather than restated, the way every amendment to this
function since 0011 has been: a hand-copied body is a body that silently loses a
check somebody added in between.
"""

from importlib import import_module

from django.db import migrations

PREVIOUS = import_module("stockledger.migrations.0015_value_damage_posting_truth").FORWARD

ANCHOR = "'P10','P11','P12','P13','P14','P15','P16','P17','P18'"

if PREVIOUS.count(ANCHOR) != 1:  # pragma: no cover - guards the text this builds on
    raise RuntimeError("the batch function's posting-kind list is no longer where this expects it")

FORWARD = PREVIOUS.replace(ANCHOR, ANCHOR + ",'P19'")


class Migration(migrations.Migration):
    dependencies = [("stockledger", "0016_live_value_basis")]
    operations = [migrations.RunSQL(FORWARD, PREVIOUS)]
