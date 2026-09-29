from decimal import Decimal

from django.db import migrations, models


def move_untouched_caps_to_the_ruled_default(apps, schema_editor):
    SellPolicy = apps.get_model("sell", "SellPolicy")
    SellPolicy.objects.filter(manual_discount_cap_percent=Decimal("0.00")).update(
        manual_discount_cap_percent=Decimal("10.00")
    )


class Migration(migrations.Migration):
    dependencies = [("sell", "0015_backfill_customers")]

    operations = [
        migrations.AddField(
            model_name="sellpolicy",
            name="manual_discount_on_offer_lines",
            field=models.BooleanField(
                default=False,
                help_text="Whether a rulebook-discounted line may also carry a manual discount.",
            ),
        ),
        migrations.RunPython(move_untouched_caps_to_the_ruled_default, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="sellpolicy",
            name="manual_discount_cap_percent",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("10.00"),
                help_text="Maximum manual discount, as a percent of MRP.",
                max_digits=5,
            ),
        ),
        migrations.AlterField(
            model_name="sale",
            name="override_kind",
            field=models.CharField(
                blank=True,
                default="",
                help_text="What was authorised - one of `sell.serializers.OVERRIDE_KINDS`: "
                "credit_note when a manager accepted a note the till could not verify.",
                max_length=40,
            ),
        ),
    ]
