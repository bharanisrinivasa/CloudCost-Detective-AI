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
        related_name="cost_anomalies",
        null=True,
        blank=True
    )
    project = models.ForeignKey(
        "accounts.Project",
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
            models.Index(fields=["project", "detected_date"]),
            models.Index(fields=["project", "severity"]),
            models.Index(fields=["project", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "anomaly_type", "detected_date", "service_name", "resource_id", "resource_name"],
                name="unique_project_anomaly_event"
            )
        ]

    @property
    def cost_increase(self):
        return self.actual_cost - self.expected_cost

    @property
    def ai_explanation(self):
        from ai_engine.models import AIExplanation
        return AIExplanation.objects.filter(user=self.user, source_type="ANOMALY", source_id=self.pk).first()

    @property
    def is_explanation_stale(self):
        explanation = self.ai_explanation
        if not explanation or explanation.status != "GENERATED":
            return False
        from ai_engine.services.explanation_service import get_anomaly_deterministic_data, calculate_input_hash
        current_data = get_anomaly_deterministic_data(self)
        current_hash = calculate_input_hash(current_data)
        return explanation.input_hash != current_hash

    def __str__(self) -> str:
        return f"{self.get_anomaly_type_display()} on {self.detected_date} ({self.severity})"

    def save(self, *args, **kwargs):
        if not hasattr(self, 'project') or self.project_id is None:
            if self.user:
                from accounts.models import Project
                project = Project.objects.filter(organization__memberships__user=self.user).first()
                if project:
                    self.project = project
        super().save(*args, **kwargs)


class WasteFinding(models.Model):
    STATUS_CHOICES = (
        ("OPEN", "Open"),
        ("REVIEWED", "Reviewed"),
        ("DISMISSED", "Dismissed"),
    )
    CONFIDENCE_CHOICES = (
        ("LOW", "Low"),
        ("MEDIUM", "Medium"),
        ("HIGH", "High"),
    )
    WASTE_TYPE_CHOICES = (
        ("PERSISTENT_LOW_COST_RESOURCE", "Persistent Low-Cost Resource"),
        ("DORMANT_COST_PATTERN", "Dormant Cost Pattern"),
        ("STALE_RESOURCE_COST", "Stale Resource Cost"),
        ("POSSIBLE_UNUSED_STORAGE", "Possible Unused Storage"),
    )
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="waste_findings",
        null=True,
        blank=True
    )
    project = models.ForeignKey(
        "accounts.Project",
        on_delete=models.CASCADE,
        related_name="waste_findings"
    )
    waste_type = models.CharField(max_length=50)
    resource_key = models.CharField(max_length=500, blank=True, default="")
    resource_id = models.CharField(max_length=500, blank=True, default="")
    resource_name = models.CharField(max_length=255, blank=True, default="")
    service_name = models.CharField(max_length=200)
    region = models.CharField(max_length=200, blank=True, default="")
    currency = models.CharField(max_length=10, default="USD")
    
    first_seen = models.DateField()
    last_seen = models.DateField()
    observation_days = models.IntegerField()
    calendar_span_days = models.IntegerField()
    coverage_ratio = models.DecimalField(max_digits=5, decimal_places=4)
    
    total_cost = models.DecimalField(max_digits=12, decimal_places=2)
    average_daily_cost = models.DecimalField(max_digits=12, decimal_places=2)
    estimated_monthly_cost = models.DecimalField(max_digits=12, decimal_places=2)
    estimated_monthly_savings = models.DecimalField(max_digits=12, decimal_places=2)
    
    confidence = models.CharField(max_length=20, choices=CONFIDENCE_CHOICES, default="LOW")
    evidence = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="OPEN")
    
    detected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["project", "waste_type"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "waste_type", "resource_key", "service_name", "currency"],
                name="unique_project_waste_event"
            )
        ]

    @property
    def ai_explanation(self):
        from ai_engine.models import AIExplanation
        return AIExplanation.objects.filter(user=self.user, source_type="WASTE", source_id=self.pk).first()

    @property
    def is_explanation_stale(self):
        explanation = self.ai_explanation
        if not explanation or explanation.status != "GENERATED":
            return False
        from ai_engine.services.explanation_service import get_waste_deterministic_data, calculate_input_hash
        current_data = get_waste_deterministic_data(self)
        current_hash = calculate_input_hash(current_data)
        return explanation.input_hash != current_hash

    def __str__(self) -> str:
        return f"{self.waste_type} - {self.resource_name or self.resource_id} ({self.status})"

    def save(self, *args, **kwargs):
        if not hasattr(self, 'project') or self.project_id is None:
            if self.user:
                from accounts.models import Project
                project = Project.objects.filter(organization__memberships__user=self.user).first()
                if project:
                    self.project = project
        super().save(*args, **kwargs)

