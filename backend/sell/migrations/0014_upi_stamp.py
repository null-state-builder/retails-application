"""The UPI stamp: how the money was proven, on every UPI tender row (#241).

Every historic UPI tender was cashier-vouched by definition - the QR charge
card and its mock adapter (#248) do not exist yet, so nothing has ever reached
this table any other way. `manual` is exactly what that means, and the backfill
says so before the check constraints start enforcing it: an `UPDATE` first,
then the three constraints, in that order, so a fresh migrate never sees a row
the constraints would refuse.
"""

from __future__ import annotations

from django.db import migrations, models


def stamp_historic_upi_manual(apps, schema_editor):
    SaleTender = apps.get_model("sell", "SaleTender")
    SaleTender.objects.filter(mode="upi").update(upi_state="manual")


def unstamp_historic_upi(apps, schema_editor):
    apps.get_model("sell", "SaleTender").objects.filter(mode="upi").update(upi_state="")


class Migration(migrations.Migration):
    dependencies = [
        ("sell", "0013_daily_check_dials_and_flag_note"),
    ]

    operations = [
        migrations.AddField(
            model_name="saletender",
            name="upi_reference",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="saletender",
            name="upi_state",
            field=models.CharField(
                blank=True,
                choices=[("confirmed", "Confirmed"), ("manual", "Manual")],
                default="",
                max_length=10,
            ),
        ),
        migrations.RunPython(stamp_historic_upi_manual, unstamp_historic_upi),
        migrations.AddConstraint(
            model_name="saletender",
            constraint=models.CheckConstraint(
                condition=models.Q(("upi_state__in", ["", "confirmed", "manual"])),
                name="ck_saletender_upi_state_values",
            ),
        ),
        migrations.AddConstraint(
            model_name="saletender",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("mode", "upi"), models.Q(("upi_state", ""), _negated=True)),
                    models.Q(models.Q(("mode", "upi"), _negated=True), ("upi_state", "")),
                    _connector="OR",
                ),
                name="ck_saletender_upi_state_iff_upi",
            ),
        ),
        migrations.AddConstraint(
            model_name="saletender",
            constraint=models.CheckConstraint(
                condition=models.Q(("upi_reference", ""), ("upi_state", "confirmed"), _connector="OR"),
                name="ck_saletender_reference_confirmed_only",
            ),
        ),
    ]
