from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0004_delete_platformkeys"),
    ]

    operations = [
        migrations.AddField(
            model_name="paystackconfig",
            name="webhook_secret",
            field=models.CharField(blank=True, max_length=512),
        ),
    ]
