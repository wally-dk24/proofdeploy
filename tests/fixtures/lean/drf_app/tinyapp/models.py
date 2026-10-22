from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=60)


class Item(models.Model):
    title = models.CharField(max_length=40)
    status = models.CharField(
        max_length=10, choices=[("draft", "Draft"), ("live", "Live")], default="draft"
    )
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL)
    dimensions = models.JSONField(null=True, default=None)
    labels = models.JSONField(default=list)
    quantity = models.IntegerField(default=1)
    created = models.DateTimeField(auto_now_add=True)
