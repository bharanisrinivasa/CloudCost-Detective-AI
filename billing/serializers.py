from rest_framework import serializers

from .models import BillingRecord, BillingUpload


class BillingUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillingUpload
        fields = [
            "id", "title", "filename", "status", "created_at", "updated_at", "error_message",
            "uploaded_by", "upload_type", "original_filename", "stored_file", "file_size",
            "upload_status", "uploaded_at", "remarks", "rows_read", "rows_imported",
            "rows_skipped", "processing_time", "processing_logs"
        ]
        read_only_fields = [
            "id", "filename", "status", "created_at", "updated_at", "error_message",
            "uploaded_by", "upload_type", "original_filename", "stored_file", "file_size",
            "upload_status", "uploaded_at", "rows_read", "rows_imported", "rows_skipped",
            "processing_time", "processing_logs"
        ]


class BillingRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillingRecord
        fields = [
            "id", "service", "resource_name", "resource_id", "compartment", "region",
            "availability_domain", "usage_start", "usage_end", "usage_quantity",
            "usage_unit", "cost", "currency", "tags", "amount", "usage_date"
        ]
