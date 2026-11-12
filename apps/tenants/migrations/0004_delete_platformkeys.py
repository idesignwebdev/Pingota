from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenants", "0003_isp_trial_ends_at")]

    operations = [
        migrations.AlterField(
            model_name="router",
            name="tunnel_address",
            field=models.GenericIPAddressField(blank=True, null=True, unique=True),
        ),
        migrations.DeleteModel(name="PlatformKeys"),
    ]
