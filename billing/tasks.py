from __future__ import annotations

import time
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import BillingRecord, BillingUpload
from .services.parser import BillingCSVParser


@shared_task
def process_upload(upload_id: int) -> str:
    start_time = time.time()
    logs = []

    def log(msg: str):
        timestamp = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
        logs.append(f"[{timestamp}] {msg}")

    try:
        upload = BillingUpload.objects.get(pk=upload_id)
    except BillingUpload.DoesNotExist:
        return "failed"

    upload.upload_status = "Processing"
    upload.status = "processing"
    upload.save(update_fields=["status", "upload_status", "updated_at"])

    log(f"Started processing BillingUpload ID: {upload_id}")
    log(f"File name: {upload.filename}")

    file_field = upload.stored_file or upload.uploaded_file
    if not file_field:
        error_msg = "No file associated with this upload."
        log(error_msg)
        upload.upload_status = "Failed"
        upload.status = "failed"
        upload.error_message = error_msg
        upload.processing_logs = "\n".join(logs)
        upload.save()
        return "failed"

    try:
        with file_field.open("rb") as binary_handle:
            text_content = binary_handle.read().decode("utf-8-sig", errors="ignore")

        # Parse CSV using the service layer parser
        parse_results = BillingCSVParser.parse(text_content, upload)
        records = parse_results["records"]
        row_logs = parse_results["logs"]
        rows_read = parse_results["rows_read"]
        rows_imported = parse_results["rows_imported"]
        rows_skipped = parse_results["rows_skipped"]

        # Append parser logs
        logs.extend(row_logs)

        # Clear and bulk insert
        with transaction.atomic():
            BillingRecord.objects.filter(upload=upload).delete()
            if records:
                BillingRecord.objects.bulk_create(records)

        log(f"Successfully database-committed {rows_imported} billing records.")

        # Save stats and update status
        runtime = time.time() - start_time
        upload.rows_read = rows_read
        upload.rows_imported = rows_imported
        upload.rows_skipped = rows_skipped
        upload.processing_time = round(runtime, 4)

        upload.upload_status = "Completed"
        upload.status = "completed"
        upload.error_message = ""
        log(f"Completed processing successfully in {upload.processing_time}s.")
        upload.processing_logs = "\n".join(logs)
        upload.save()
        return "completed"

    except Exception as exc:
        runtime = time.time() - start_time
        log(f"Fatal exception during process: {str(exc)}")
        upload.upload_status = "Failed"
        upload.status = "failed"
        upload.error_message = str(exc)
        upload.rows_read = 0
        upload.rows_imported = 0
        upload.rows_skipped = 0
        upload.processing_time = round(runtime, 4)
        upload.processing_logs = "\n".join(logs)
        upload.save()
        return "failed"

