from django.db import models
from django.db.models import UniqueConstraint, Index
from accounts.models import Project

INVENTORY_STATUS_CHOICES = (
    ("PRESENT", "Present"),
    ("ABSENT", "Absent"),
    ("UNKNOWN", "Unknown"),
)

class OCIConnection(models.Model):
    project = models.OneToOneField(
        "accounts.Project",
        on_delete=models.CASCADE,
        related_name="oci_connection",
        null=True,
        blank=True
    )
    name = models.CharField(max_length=200)
    tenancy_ocid = models.CharField(max_length=255, default="")
    user_ocid = models.CharField(max_length=255, default="")
    fingerprint = models.CharField(max_length=255, default="")
    private_key_encrypted = models.TextField(default="")
    region = models.CharField(max_length=100, default="us-ashburn-1")
    compartment_ocid = models.CharField(max_length=255, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.project.name})"


class OCIComputeInstance(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="oci_instances")
    connection = models.ForeignKey(OCIConnection, on_delete=models.CASCADE, related_name="instances")
    ocid = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    state = models.CharField(max_length=100)
    shape = models.CharField(max_length=100)
    ocpus = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    memory_in_gbs = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    region = models.CharField(max_length=100)
    compartment_id = models.CharField(max_length=255)
    inventory_status = models.CharField(
        max_length=20,
        choices=INVENTORY_STATUS_CHOICES,
        default="PRESENT",
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["project", "ocid"], name="unique_project_compute_ocid")
        ]
        indexes = [
            Index(fields=["project", "ocid"]),
            Index(fields=["project", "state"]),
            Index(fields=["project", "inventory_status"]),
        ]

    def __str__(self) -> str:
        return f"Compute: {self.name} ({self.state})"


class OCIVolume(models.Model):
    VOLUME_TYPE_CHOICES = (
        ("BOOT", "Boot Volume"),
        ("BLOCK", "Block Volume"),
    )
    
    ATTACHMENT_STATE_CHOICES = (
        ("ATTACHED", "Attached"),
        ("DETACHED", "Detached"),
        ("UNKNOWN", "Unknown"),
    )

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="oci_volumes")
    connection = models.ForeignKey(OCIConnection, on_delete=models.CASCADE, related_name="volumes")
    ocid = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    volume_type = models.CharField(max_length=50, choices=VOLUME_TYPE_CHOICES)
    state = models.CharField(max_length=100)
    size_in_gbs = models.BigIntegerField()
    attachment_state = models.CharField(max_length=50, choices=ATTACHMENT_STATE_CHOICES, default="UNKNOWN")
    attached_instance_id = models.CharField(max_length=255, null=True, blank=True)
    region = models.CharField(max_length=100)
    compartment_id = models.CharField(max_length=255)
    inventory_status = models.CharField(
        max_length=20,
        choices=INVENTORY_STATUS_CHOICES,
        default="PRESENT",
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["project", "ocid"], name="unique_project_volume_ocid")
        ]
        indexes = [
            Index(fields=["project", "ocid"]),
            Index(fields=["project", "attachment_state"]),
            Index(fields=["project", "inventory_status"]),
        ]

    def __str__(self) -> str:
        return f"Volume: {self.name} ({self.volume_type} - {self.attachment_state})"


class OCIObjectStorageBucket(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="oci_buckets")
    connection = models.ForeignKey(OCIConnection, on_delete=models.CASCADE, related_name="buckets")
    name = models.CharField(max_length=255)
    namespace = models.CharField(max_length=255)
    approximate_size = models.BigIntegerField(null=True, blank=True)
    approximate_count = models.BigIntegerField(null=True, blank=True)
    storage_tier = models.CharField(max_length=100, null=True, blank=True)
    region = models.CharField(max_length=100)
    compartment_id = models.CharField(max_length=255)
    inventory_status = models.CharField(
        max_length=20,
        choices=INVENTORY_STATUS_CHOICES,
        default="PRESENT",
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["project", "namespace", "name"], name="unique_project_bucket_identity")
        ]
        indexes = [
            Index(fields=["project", "namespace", "name"]),
            Index(fields=["project", "inventory_status"]),
        ]

    def __str__(self) -> str:
        return f"Bucket: {self.name} in {self.namespace}"


class OCIPublicIp(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="oci_public_ips")
    connection = models.ForeignKey(OCIConnection, on_delete=models.CASCADE, related_name="public_ips")
    ocid = models.CharField(max_length=255)
    ip_address = models.CharField(max_length=100)
    scope = models.CharField(max_length=100) # REGIONAL, AD_SPECIFIC
    lifecycle_state = models.CharField(max_length=100)
    assigned_entity_type = models.CharField(max_length=100, null=True, blank=True)
    assigned_entity_id = models.CharField(max_length=255, null=True, blank=True)
    is_orphan = models.BooleanField(default=False)
    region = models.CharField(max_length=100)
    compartment_id = models.CharField(max_length=255)
    inventory_status = models.CharField(
        max_length=20,
        choices=INVENTORY_STATUS_CHOICES,
        default="PRESENT",
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["project", "ocid"], name="unique_project_public_ip_ocid")
        ]
        indexes = [
            Index(fields=["project", "ocid"]),
            Index(fields=["project", "is_orphan"]),
            Index(fields=["project", "inventory_status"]),
        ]

    def __str__(self) -> str:
        return f"PublicIP: {self.ip_address} (Orphan: {self.is_orphan})"


class OCILoadBalancer(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="oci_load_balancers")
    connection = models.ForeignKey(OCIConnection, on_delete=models.CASCADE, related_name="load_balancers")
    ocid = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    shape = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    is_private = models.BooleanField(default=False)
    ip_addresses = models.JSONField(default=list, blank=True)
    region = models.CharField(max_length=100)
    compartment_id = models.CharField(max_length=255)
    inventory_status = models.CharField(
        max_length=20,
        choices=INVENTORY_STATUS_CHOICES,
        default="PRESENT",
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["project", "ocid"], name="unique_project_lb_ocid")
        ]
        indexes = [
            Index(fields=["project", "ocid"]),
            Index(fields=["project", "inventory_status"]),
        ]

    def __str__(self) -> str:
        return f"LoadBalancer: {self.name} ({self.state})"


class OCIResourceMetricSummary(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="oci_metric_summaries")
    connection = models.ForeignKey(OCIConnection, on_delete=models.CASCADE, null=True, blank=True, related_name="metric_summaries")
    resource_id = models.CharField(max_length=255, db_index=True)
    metric_name = models.CharField(max_length=100, db_index=True)
    date = models.DateField(db_index=True)
    average_value = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    maximum_value = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    minimum_value = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    sample_count = models.IntegerField(default=0)
    coverage_ratio = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["project", "resource_id", "metric_name", "date"], name="unique_project_resource_metric_date")
        ]

    def __str__(self) -> str:
        return f"Metric: {self.metric_name} for {self.resource_id} on {self.date}"


class OCISyncLog(models.Model):
    SYNC_TYPE_CHOICES = (
        ("ALL", "Full Sync"),
        ("COST", "Cost Only"),
        ("INVENTORY", "Inventory Only"),
        ("METRICS", "Metrics Only"),
    )
    
    STATUS_CHOICES = (
        ("PROCESSING", "Processing"),
        ("COMPLETED", "Completed"),
        ("PARTIAL", "Completed with Warnings"),
        ("FAILED", "Failed"),
    )

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="oci_sync_logs")
    connection = models.ForeignKey(OCIConnection, on_delete=models.CASCADE, related_name="sync_logs")
    sync_type = models.CharField(max_length=50, choices=SYNC_TYPE_CHOICES)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="PROCESSING")
    records_created = models.IntegerField(default=0)
    records_updated = models.IntegerField(default=0)
    warning_summary = models.TextField(blank=True, default="")
    error_summary = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            Index(fields=["project", "started_at"]),
        ]

    def __str__(self) -> str:
        return f"Sync {self.sync_type} ({self.status}) at {self.started_at}"
