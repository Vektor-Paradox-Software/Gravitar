from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("blog", "0001_initial")]
    operations = [
        migrations.AlterField(
            model_name="widget", name="name", field=models.CharField(max_length=100)
        ),
    ]
