from __future__ import annotations

import os

from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError


def validate_csv_upload(file):
    """Validator to ensure file is a CSV and is under 100MB."""
    ext = os.path.splitext(file.name)[1].lower()
    if ext != '.csv':
        raise ValidationError("Unsupported file format. Only CSV files (.csv) are allowed.")
    
    # 100 MB = 100 * 1024 * 1024 bytes
    max_size = 100 * 1024 * 1024
    if file.size > max_size:
        raise ValidationError("File size exceeds the maximum limit of 100 MB.")


class BillingUpload(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    )

    UPLOAD_TYPE_CHOICES = (
        ("Billing Report", "Billing Report"),
        ("Usage Report", "Usage Report"),
    )

    UPLOAD_STATUS_CHOICES = (
        ("Uploaded", "Uploaded"),
        ("Processing", "Processing"),
        ("Completed", "Completed"),
        ("Failed", "Failed"),
    )

    # Legacy fields (kept for DRF compatibility)
    title = models.CharField(max_length=200, blank=True, default="")
    uploaded_file = models.FileField(upload_to="uploads/", blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", blank=True)
    created_at = models.DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, blank=True, null=True)
    error_message = models.TextField(blank=True, default="")

    # New Module 2 fields
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="billing_uploads",
        null=True,
        blank=True
    )
    project = models.ForeignKey(
        "accounts.Project",
        on_delete=models.CASCADE,
        related_name="billing_uploads"
    )
    upload_type = models.CharField(
        max_length=50,
        choices=UPLOAD_TYPE_CHOICES,
        default="Billing Report"
    )
    original_filename = models.CharField(max_length=255, blank=True, default="")
    stored_file = models.FileField(
        upload_to="uploads/billing/",
        validators=[validate_csv_upload],
        null=True,
        blank=True
    )
    file_size = models.BigIntegerField(default=0)
    upload_status = models.CharField(
        max_length=50,
        choices=UPLOAD_STATUS_CHOICES,
        default="Uploaded"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    remarks = models.TextField(blank=True, default="")

    # Module 3 calculations & logs
    rows_read = models.IntegerField(default=0)
    rows_imported = models.IntegerField(default=0)
    rows_skipped = models.IntegerField(default=0)
    processing_time = models.FloatField(default=0.0)
    processing_logs = models.TextField(blank=True, default="")

    def __str__(self) -> str:
        return self.original_filename or self.title or f"Upload {self.pk}"

    @property
    def filename(self) -> str:
        if self.stored_file:
            return os.path.basename(self.stored_file.name)
        return os.path.basename(self.uploaded_file.name) if self.uploaded_file else ""

    def save(self, *args, **kwargs):
        if not self.upload_status:
            self.upload_status = "Pending"
        if not self.upload_type:
            self.upload_type = "Billing Report"

        if not hasattr(self, 'project') or self.project_id is None:
            from accounts.models import Project
            project = None
            if self.uploaded_by:
                project = Project.objects.filter(organization__memberships__user=self.uploaded_by).first()
            if not project:
                project = Project.objects.first()
            if not project:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                dummy_user, _ = User.objects.get_or_create(username="dummy_tenant_system", email="dummy@example.com")
                from accounts.services.tenant_service import provision_default_tenant
                provision_default_tenant(dummy_user)
                project = Project.objects.first()
            self.project = project
        # Bridge legacy uploaded_file to stored_file if needed
        if self.uploaded_file and not self.stored_file:
            self.stored_file = self.uploaded_file

        if self.stored_file:
            if not self.original_filename:
                self.original_filename = os.path.basename(self.stored_file.name)
            if not self.file_size:
                try:
                    self.file_size = self.stored_file.size
                except Exception:
                    self.file_size = 0
            if not self.uploaded_file:
                self.uploaded_file = self.stored_file
            if not self.title:
                self.title = self.original_filename
        super().save(*args, **kwargs)


class BillingRecord(models.Model):
    upload = models.ForeignKey(BillingUpload, related_name="billing_records", on_delete=models.CASCADE)
    
    # Extended fields for OCI Cost Report columns
    service = models.CharField(max_length=200)
    resource_name = models.CharField(max_length=255, blank=True, default="")
    resource_id = models.CharField(max_length=500)
    compartment = models.CharField(max_length=200)
    region = models.CharField(max_length=200)
    availability_domain = models.CharField(max_length=100, blank=True, default="")
    usage_start = models.DateTimeField(null=True, blank=True)
    usage_end = models.DateTimeField(null=True, blank=True)
    usage_quantity = models.DecimalField(max_digits=18, decimal_places=6, default=0.0)
    usage_unit = models.CharField(max_length=50, blank=True, default="")
    cost = models.DecimalField(max_digits=12, decimal_places=2, default=0.0)
    currency = models.CharField(max_length=10, blank=True, default="USD")
    tags = models.TextField(blank=True, default="")
    
    # Legacy fields kept for backward compatibility (synced via .save())
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    usage_date = models.DateField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.service} - {self.cost or self.amount}"

    def save(self, *args, **kwargs):
        # Synchronize cost/amount and usage_start/usage_date
        if self.cost is not None and (self.amount is None or self.amount == 0):
            self.amount = self.cost
        if self.amount is not None and (self.cost is None or self.cost == 0):
            self.cost = self.amount
        
        if self.usage_start and not self.usage_date:
            if hasattr(self.usage_start, 'date'):
                self.usage_date = self.usage_start.date()
            else:
                self.usage_date = self.usage_start
        elif self.usage_date and not self.usage_start:
            import datetime
            dt = datetime.datetime.combine(self.usage_date, datetime.time.min)
            from django.conf import settings
            if getattr(settings, 'USE_TZ', False):
                from django.utils.timezone import make_aware
                try:
                    self.usage_start = make_aware(dt)
                except Exception:
                    self.usage_start = dt
            else:
                self.usage_start = dt
            
        super().save(*args, **kwargs)
