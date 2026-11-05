from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Post",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("duration", models.DurationField()),
            ],
        ),
    ]
