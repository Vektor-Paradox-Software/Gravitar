from django.db import models


class Widget(models.Model):
    name = models.CharField(max_length=50)
    legacy = models.CharField(max_length=20, null=True)


class Gadget(models.Model):
    title = models.CharField(max_length=50)
    notes = models.CharField(max_length=200, null=True)
