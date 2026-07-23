from django.db import models
from django.conf import settings
from billing.models import BillingUpload

class CostAnomaly(models.Model):
    ANOMALY_TYPE_CHOICES = (
        ("DAILY_SPIKE", "Daily Spike"),
        ("SERVICE_SPIKE", "Service Spike"),
        ("RESOURCE_SPIKE", "Resource Spike"),
        ("UNUSUAL_GROWTH", "Unusual Growth"),
    )
    
    SEVERITY_CHOICES = (
        ("LOW", "Low"),
        ("MEDIUM", "Medium"),
        ("HIGH", "High"),
        ("CRITICAL", "Critical"),
    )
    
    STATUS_CHOICES = (
        ("OPEN", "Open"),
        ("REVIEWED", "Reviewed"),
        ("DISMISSED", "Dismissed"),
    )
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="cost_anomalies"
    )
    
    billing_upload = models.ForeignKey(
        BillingUpload,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cost_anomalies"
    )
    
    anomaly_type = models.CharField(max_length=50, choices=ANOMALY_TYPE_CHOICES)
    detected_date = models.DateField()
    
    service_name = models.CharField(max_length=200, blank=True, default="")
    resource_id = models.CharField(max_length=500, blank=True, default="")
    resource_name = models.CharField(max_length=255, blank=True, default="")
    region = models.CharField(max_length=200, blank=True, default="")
    
    actual_cost = models.DecimalField(max_digits=12, decimal_places=2)
    expected_cost = models.DecimalField(max_digits=12, decimal_places=2)
    deviation_percentage = models.DecimalField(max_digits=10, decimal_places=2)
    z_score = models.FloatField(null=True, blank=True)
    
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default="LOW")
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="OPEN")
    
    detected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        indexes = [
            models.Index(fields=["user", "detected_date"]),
            models.Index(fields=["user", "severity"]),
            models.Index(fields=["user", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "anomaly_type", "detected_date", "service_name", "resource_id", "resource_name"],
                name="unique_user_anomaly_event"
            )
        ]

    @property
    def cost_increase(self):
        return self.actual_cost - self.expected_cost

    def __str__(self) -> str:
        return f"{self.get_anomaly_type_display()} on {self.detected_date} ({self.severity})"
