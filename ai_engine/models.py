from django.db import models
from django.conf import settings


class Recommendation(models.Model):
    title = models.CharField(max_length=200)
    reason = models.TextField()
    evidence = models.TextField(blank=True, default="")
    confidence = models.FloatField(default=0.0)
    estimated_savings = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    risk_level = models.CharField(max_length=20, default="medium")
    created_at = models.DateTimeField(auto_now_add=True)


class AIExplanation(models.Model):
    SOURCE_TYPE_CHOICES = (
        ("ANOMALY", "Anomaly"),
        ("WASTE", "Waste"),
    )
    STATUS_CHOICES = (
        ("GENERATED", "Generated"),
        ("FAILED", "Failed"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_explanations"
    )
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPE_CHOICES)
    source_id = models.PositiveIntegerField()

    # Structured response fields
    summary = models.TextField(blank=True, default="")
    why_flagged = models.TextField(blank=True, default="")
    evidence_summary = models.TextField(blank=True, default="")  # Semicolon or newline delimited
    financial_impact = models.TextField(blank=True, default="")
    confidence_explanation = models.TextField(blank=True, default="")
    recommended_next_step = models.TextField(blank=True, default="")
    limitations = models.TextField(blank=True, default="")

    model_name = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="GENERATED")
    error_message = models.TextField(blank=True, default="")
    input_hash = models.CharField(max_length=64, db_index=True)

    generated_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "source_type", "source_id"]),
            models.Index(fields=["input_hash"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "source_type", "source_id"],
                name="unique_user_source_explanation"
            )
        ]

    @property
    def source_object(self):
        from analytics.models import CostAnomaly, WasteFinding
        if self.source_type == "ANOMALY":
            return CostAnomaly.objects.filter(pk=self.source_id, user=self.user).first()
        elif self.source_type == "WASTE":
            return WasteFinding.objects.filter(pk=self.source_id, user=self.user).first()
        return None


    def __str__(self) -> str:
        return f"AIExplanation for {self.source_type} #{self.source_id} ({self.status})"


class ChatSession(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_sessions"
    )
    title = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "updated_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.user.username})"


class ChatMessage(models.Model):
    ROLE_CHOICES = (
        ("USER", "User"),
        ("ASSISTANT", "Assistant"),
    )
    session = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name="messages"
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    intent = models.CharField(max_length=50, blank=True, default="")
    query_plan = models.JSONField(blank=True, null=True)
    deterministic_context = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["session", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.role} in {self.session.title}: {self.content[:30]}"


