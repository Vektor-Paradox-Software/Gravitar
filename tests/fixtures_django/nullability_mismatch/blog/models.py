from django.db import models


class Post(models.Model):
    subtitle = models.CharField(max_length=50, null=True)
