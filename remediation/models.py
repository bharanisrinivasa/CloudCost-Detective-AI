from django.db import models


class RemediationAction(models.Model):
    title = models.CharField(max_length=200)
    approval_status = models.CharField(max_length=20, default="pending")
    action_type = models.CharField(max_length=50, default="resize")
    created_at = models.DateTimeField(auto_now_add=True)
