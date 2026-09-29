from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    # pg_trgm is a "trusted" extension on PostgreSQL 13+, so the non-superuser DB
    # owner Render provisions can create it. It powers the read-only /suggest endpoint
    # (fuzzy Master-value pre-fills); nothing writes through it.
    dependencies = [
        ("ptmapper", "0009_lookupproposal"),
    ]

    operations = [
        TrigramExtension(),
    ]
