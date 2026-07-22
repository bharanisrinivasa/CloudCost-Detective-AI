from django.db import models


class Recommendation(models.Model):
    title = models.CharField(max_length=200)
    reason = models.TextField()
    evidence = models.TextField(blank=True, default="")
    confidence = models.FloatField(default=0.0)
    estimated_savings = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    risk_level = models.CharField(max_length=20, default="medium")
    created_at = models.DateTimeField(auto_now_add=True)
