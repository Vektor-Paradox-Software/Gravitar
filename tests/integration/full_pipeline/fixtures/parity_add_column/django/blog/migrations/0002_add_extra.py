from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("blog", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="widget", name="extra", field=models.CharField(max_length=50, null=True)
        ),
    ]
