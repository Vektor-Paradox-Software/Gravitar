from django.db import models


class Post(models.Model):
    duration = models.DurationField()
