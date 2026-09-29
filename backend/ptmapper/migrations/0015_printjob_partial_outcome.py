# GSA-T11: partial print outcomes need their own status/outcome value (design §5.8).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ptmapper', '0014_openingmanifest_openingmanifestversion_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='printevent',
            name='outcome',
            field=models.CharField(choices=[('attempted', 'Attempted'), ('confirmed', 'Confirmed'), ('partial', 'Partial'), ('failed', 'Failed'), ('unknown', 'Unknown'), ('scan_verified', 'Scan Verified')], max_length=14),
        ),
        migrations.AlterField(
            model_name='printjob',
            name='status',
            field=models.CharField(choices=[('prepared', 'Prepared'), ('attempted', 'Attempted'), ('confirmed', 'Confirmed'), ('partial', 'Partial'), ('failed', 'Failed'), ('unknown', 'Unknown')], default='prepared', max_length=10),
        ),
    ]
