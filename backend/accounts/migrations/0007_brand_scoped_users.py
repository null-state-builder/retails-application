"""Brand-scoped users (issue #88).

A brand manager's scope is a set of brands across every store, not a set of
stores — so the top bar offers them a brand filter where everyone else gets a
business-unit list. Adds the `brand` scope and the brands a user is assigned.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_store_manager_staff_manage'),
        ('masters', '0004_seed_default_category_margin'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='brands',
            field=models.ManyToManyField(blank=True, related_name='users', to='masters.brand'),
        ),
        migrations.AlterField(
            model_name='user',
            name='scope_type',
            field=models.CharField(choices=[('all', 'All (network-wide)'), ('entity', 'Legal entity'), ('region', 'Region / state'), ('store_group', 'Store group'), ('store', 'Single store'), ('brand', 'Assigned brands (across stores)')], default='store', max_length=20),
        ),
    ]
