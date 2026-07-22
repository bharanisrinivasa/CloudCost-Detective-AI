from django.db import models


class OCIConnection(models.Model):
    name = models.CharField(max_length=200)
    region = models.CharField(max_length=100, default="us-ashburn-1")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
