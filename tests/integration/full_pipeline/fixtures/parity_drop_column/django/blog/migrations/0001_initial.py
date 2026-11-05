from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Widget",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("name", models.CharField(max_length=50)),
                ("legacy", models.CharField(max_length=20, null=True)),
            ],
        ),
    ]
