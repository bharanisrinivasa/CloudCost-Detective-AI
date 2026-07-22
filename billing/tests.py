from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
import datetime
from decimal import Decimal

from .models import BillingUpload, BillingRecord, validate_csv_upload
from .tasks import process_upload
from .services.validator import CSVHeaderValidator
from .services.parser import BillingCSVParser
from .services.aggregator import BillingCostAggregator

User = get_user_model()


class BillingUploadProcessingTests(TestCase):
    def test_upload_processing_creates_records_and_completes(self):
        csv_bytes = b"service,region,compartment,resource_id,amount,usage_date,tags\nCompute,us-phoenix-1,dev,ocid1.instance,1250.50,2026-07-01,env:dev\nStorage,us-ashburn-1,prod,ocid1.bucket,500.00,2026-07-01,team:ops\n"
        upload = BillingUpload.objects.create(
            title="sample-billing",
            uploaded_file=SimpleUploadedFile("billing.csv", csv_bytes, content_type="text/csv"),
        )

        process_upload(upload.id)

        upload.refresh_from_db()
        self.assertEqual(upload.status, "completed")
        self.assertEqual(upload.upload_status, "Completed")
        self.assertGreaterEqual(upload.billing_records.count(), 2)


class BillingUploadModule2Tests(TestCase):
    """Unit tests for Module 2 & 3: Billing & Usage CSV uploads, validation, and views."""

    def setUp(self):
        self.user1 = User.objects.create_user(username="user1", email="user1@example.com", password="password123")
        self.user2 = User.objects.create_user(username="user2", email="user2@example.com", password="password123")
        self.staff_user = User.objects.create_user(username="staff", email="staff@example.com", password="password123", is_staff=True)

        self.upload_billing_url = reverse("upload-billing")
        self.upload_usage_url = reverse("upload-usage")
        self.upload_history_url = reverse("upload-history")

        # Valid CSV header and content to pass the new service parser validator
        self.valid_csv = SimpleUploadedFile(
            "valid.csv",
            b"service,region,compartment,resource_id,amount,usage_date,tags\nCompute,us-phoenix-1,dev,ocid1.instance,1250.50,2026-07-01,env:dev\n",
            content_type="text/csv"
        )
        self.invalid_pdf = SimpleUploadedFile("invalid.pdf", b"%PDF-1.4...", content_type="application/pdf")
        self.invalid_xlsx = SimpleUploadedFile("invalid.xlsx", b"PK...", content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def test_csv_validator_extension_rejection(self):
        """Test that validator rejects non-CSV files and accepts CSV files."""
        # Valid CSV
        try:
            validate_csv_upload(self.valid_csv)
        except ValidationError:
            self.fail("validate_csv_upload raised ValidationError unexpectedly on valid CSV.")

        # Invalid PDF
        with self.assertRaises(ValidationError) as context:
            validate_csv_upload(self.invalid_pdf)
        self.assertIn("Only CSV files (.csv) are allowed", str(context.exception))

        # Invalid XLSX
        with self.assertRaises(ValidationError) as context:
            validate_csv_upload(self.invalid_xlsx)
        self.assertIn("Only CSV files (.csv) are allowed", str(context.exception))

    def test_csv_validator_size_rejection(self):
        """Test that files exceeding 100 MB are rejected."""
        large_file = SimpleUploadedFile("large.csv", b"col1\nval1", content_type="text/csv")
        large_file.size = 100 * 1024 * 1024 + 1  # 100 MB + 1 byte
        
        with self.assertRaises(ValidationError) as context:
            validate_csv_upload(large_file)
        self.assertIn("File size exceeds the maximum limit of 100 MB", str(context.exception))

    def test_anonymous_user_redirect(self):
        """Test that unauthenticated users cannot access upload views."""
        response = self.client.get(self.upload_billing_url)
        self.assertRedirects(response, f"/accounts/login/?next={self.upload_billing_url}")

        response = self.client.get(self.upload_usage_url)
        self.assertRedirects(response, f"/accounts/login/?next={self.upload_usage_url}")

        response = self.client.get(self.upload_history_url)
        self.assertRedirects(response, f"/accounts/login/?next={self.upload_history_url}")

    def test_billing_upload_success(self):
        """Test successful upload of Billing CSV by authenticated user."""
        self.client.login(username="user1", password="password123")
        
        response = self.client.post(self.upload_billing_url, {
            "stored_file": self.valid_csv,
            "remarks": "Test billing report"
        })
        self.assertRedirects(response, self.upload_history_url)

        # Check database
        upload = BillingUpload.objects.get(original_filename="valid.csv")
        self.assertEqual(upload.uploaded_by, self.user1)
        self.assertEqual(upload.upload_type, "Billing Report")
        self.assertEqual(upload.upload_status, "Completed")
        self.assertEqual(upload.remarks, "Test billing report")
        self.assertGreater(upload.file_size, 0)
        self.assertTrue(upload.stored_file.name.startswith("uploads/billing/valid"))

        # Cleanup physical file
        if upload.stored_file:
            upload.stored_file.delete(save=False)

    def test_usage_upload_success(self):
        """Test successful upload of Usage CSV by authenticated user."""
        self.client.login(username="user1", password="password123")
        
        response = self.client.post(self.upload_usage_url, {
            "stored_file": self.valid_csv,
            "remarks": "Test usage report"
        })
        self.assertRedirects(response, self.upload_history_url)

        # Check database
        upload = BillingUpload.objects.get(original_filename="valid.csv")
        self.assertEqual(upload.uploaded_by, self.user1)
        self.assertEqual(upload.upload_type, "Usage Report")
        self.assertEqual(upload.upload_status, "Completed")
        
        # Cleanup physical file
        if upload.stored_file:
            upload.stored_file.delete(save=False)

    def test_upload_ownership_delete_restrictions(self):
        """Test uploader ownership rules for deletion."""
        self.client.login(username="user1", password="password123")
        
        # Upload file as user1
        upload = BillingUpload.objects.create(
            uploaded_by=self.user1,
            upload_type="Billing Report",
            original_filename="user1_report.csv",
            stored_file=SimpleUploadedFile("user1_report.csv", b"col1\nval1", content_type="text/csv")
        )
        delete_url = reverse("upload-delete", args=[upload.pk])

        # Login as user2 and attempt deletion (should fail)
        self.client.login(username="user2", password="password123")
        response = self.client.post(delete_url)
        self.assertRedirects(response, self.upload_history_url)
        self.assertTrue(BillingUpload.objects.filter(pk=upload.pk).exists())

        # Login as uploader user1 and attempt deletion (should succeed)
        self.client.login(username="user1", password="password123")
        response = self.client.post(delete_url)
        self.assertRedirects(response, self.upload_history_url)
        self.assertFalse(BillingUpload.objects.filter(pk=upload.pk).exists())

    def test_staff_user_delete_success(self):
        """Test that staff user can delete other user's uploads."""
        # Upload file as user1
        upload = BillingUpload.objects.create(
            uploaded_by=self.user1,
            upload_type="Billing Report",
            original_filename="user1_report.csv",
            stored_file=SimpleUploadedFile("user1_report.csv", b"col1\nval1", content_type="text/csv")
        )
        delete_url = reverse("upload-delete", args=[upload.pk])

        # Login as staff and delete
        self.client.login(username="staff", password="password123")
        response = self.client.post(delete_url)
        self.assertRedirects(response, self.upload_history_url)
        self.assertFalse(BillingUpload.objects.filter(pk=upload.pk).exists())


class BillingUploadModule3ServiceTests(TestCase):
    """Unit tests specifically covering Module 3 services and processing flow."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", email="testuser@example.com", password="password123")

    def test_csv_header_validator_invalid_headers(self):
        """Test that validator rejects headers that are missing essential columns."""
        invalid_headers = ["region", "compartment", "resource_id", "tags"]
        with self.assertRaises(ValidationError):
            CSVHeaderValidator.validate(invalid_headers)

        valid_headers = ["service", "amount"]
        col_map = CSVHeaderValidator.validate(valid_headers)
        self.assertEqual(col_map["service"], 0)
        self.assertEqual(col_map["cost"], 1)

    def test_csv_parser_graceful_skips(self):
        """Test that parser skips malformed rows and logs warnings."""
        upload = BillingUpload.objects.create(
            uploaded_by=self.user,
            upload_type="Billing Report",
            original_filename="sample_with_errors.csv"
        )
        # Row 1: Valid OCI cost row
        # Row 2: Malformed (invalid cost number)
        # Row 3: Missing service column
        # Row 4: Missing usage date
        csv_data = (
            "lineItem/service,product/resourceId,cost/myCost,lineItem/intervalUsageStart\n"
            "Compute,instance-ocid,150.50,2026-07-22T08:00:00Z\n"
            "Compute,instance-ocid,BAD_COST,2026-07-22T09:00:00Z\n"
            ",instance-ocid2,50.00,2026-07-22T10:00:00Z\n"
            "Storage,instance-ocid3,12.00,\n"
        )
        parse_results = BillingCSVParser.parse(csv_data, upload)
        records = parse_results["records"]
        
        self.assertEqual(len(records), 1)
        self.assertEqual(parse_results["rows_read"], 4)
        self.assertEqual(parse_results["rows_imported"], 1)
        self.assertEqual(parse_results["rows_skipped"], 3)
        self.assertEqual(records[0].service, "Compute")
        self.assertEqual(records[0].cost, Decimal("150.50"))

    def test_csv_parser_duplicate_skips(self):
        """Test duplicate row skipping in the CSV parsing step."""
        upload = BillingUpload.objects.create(
            uploaded_by=self.user,
            upload_type="Billing Report",
            original_filename="duplicates.csv"
        )
        # Rows 1 and 2 are identical records
        csv_data = (
            "service,resource_id,amount,usage_date\n"
            "Database,db-ocid,45.10,2026-07-22\n"
            "Database,db-ocid,45.10,2026-07-22\n"
            "Database,db-ocid2,45.10,2026-07-22\n"
        )
        parse_results = BillingCSVParser.parse(csv_data, upload)
        records = parse_results["records"]
        
        self.assertEqual(len(records), 2)
        self.assertEqual(parse_results["rows_imported"], 2)
        self.assertEqual(parse_results["rows_skipped"], 1)

    def test_cost_aggregator_calculations(self):
        """Test aggregate cost calculator service functions."""
        upload = BillingUpload.objects.create(
            uploaded_by=self.user,
            upload_type="Billing Report",
            original_filename="calc.csv"
        )
        # Create billing records manually
        from django.utils import timezone
        r1 = BillingRecord.objects.create(
            upload=upload, service="Compute", region="us-phoenix-1", compartment="dev",
            resource_id="id1", cost=Decimal("100.00"),
            usage_start=timezone.make_aware(datetime.datetime(2026, 7, 1, 10, 0, 0))
        )
        r2 = BillingRecord.objects.create(
            upload=upload, service="Compute", region="us-ashburn-1", compartment="prod",
            resource_id="id2", cost=Decimal("50.00"),
            usage_start=timezone.make_aware(datetime.datetime(2026, 7, 1, 12, 0, 0))
        )
        r3 = BillingRecord.objects.create(
            upload=upload, service="Storage", region="us-phoenix-1", compartment="dev",
            resource_id="id3", cost=Decimal("25.50"),
            usage_start=timezone.make_aware(datetime.datetime(2026, 7, 2, 8, 0, 0))
        )

        queryset = BillingRecord.objects.filter(upload=upload)

        service_costs = BillingCostAggregator.calculate_service_costs(queryset)
        daily_costs = BillingCostAggregator.calculate_daily_costs(queryset)
        monthly_costs = BillingCostAggregator.calculate_monthly_costs(queryset)
        region_costs = BillingCostAggregator.calculate_region_costs(queryset)

        self.assertEqual(service_costs["Compute"], 150.0)
        self.assertEqual(service_costs["Storage"], 25.5)

        self.assertEqual(daily_costs["2026-07-01"], 150.0)
        self.assertEqual(daily_costs["2026-07-02"], 25.5)

        self.assertEqual(monthly_costs["2026-07"], 175.5)

        self.assertEqual(region_costs["us-phoenix-1"], 125.5)
        self.assertEqual(region_costs["us-ashburn-1"], 50.0)


