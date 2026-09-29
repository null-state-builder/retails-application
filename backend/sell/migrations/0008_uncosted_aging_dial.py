"""How long a sold-before-inward line may wait before somebody is told (#186).

A dial rather than a constant (Rule 12): three days is a starting number nobody
has ruled on, and head office moves it without a deploy. Zero is allowed - "flag
it the same day" is a legitimate setting - and negative is not.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sell', '0007_heldbill'),
    ]

    operations = [
        migrations.AddField(
            model_name='sellpolicy',
            name='uncosted_aging_days',
            field=models.IntegerField(default=3, help_text="How many days a sold-before-inward line may wait to be costed before the store's exception list carries it."),
        ),
        migrations.AddConstraint(
            model_name='sellpolicy',
            constraint=models.CheckConstraint(condition=models.Q(('uncosted_aging_days__gte', 0)), name='ck_sellpolicy_aging_is_not_negative'),
        ),
    ]
