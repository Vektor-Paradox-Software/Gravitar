from django.db import models


class Post(models.Model):
    title = models.CharField(max_length=200)
    subtitle = models.CharField(max_length=200, null=True)
