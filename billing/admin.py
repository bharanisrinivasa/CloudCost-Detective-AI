from django.contrib import admin
from .models import BillingUpload, BillingRecord


class BillingUploadAdmin(admin.ModelAdmin):
    """Admin configuration for BillingUpload model."""
    list_display = ("original_filename", "upload_type", "uploaded_by", "upload_status", "uploaded_at")
    list_filter = ("upload_type", "upload_status", "uploaded_at")
    search_fields = ("original_filename", "remarks", "uploaded_by__username")
    readonly_fields = ("file_size", "uploaded_at")


admin.site.register(BillingUpload, BillingUploadAdmin)
admin.site.register(BillingRecord)

