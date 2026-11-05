from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Post",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("title", models.CharField(max_length=200)),
                ("body", models.TextField()),
            ],
        ),
    ]
