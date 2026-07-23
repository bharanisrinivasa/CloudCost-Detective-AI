from decimal import Decimal
import datetime
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from billing.models import BillingUpload, BillingRecord

User = get_user_model()

class DashboardTests(TestCase):
    def setUp(self):
        # Create users
        self.user_a = User.objects.create_user(username="usera", email="a@example.com", password="password123")
        self.user_b = User.objects.create_user(username="userb", email="b@example.com", password="password123")
        
        self.dashboard_url = reverse("dashboard-home")
        
        # Create uploads for user A
        self.upload_a = BillingUpload.objects.create(
            uploaded_by=self.user_a,
            upload_type="Billing Report",
            original_filename="user_a_billing.csv"
        )
        
        # Create uploads for user B
        self.upload_b = BillingUpload.objects.create(
            uploaded_by=self.user_b,
            upload_type="Billing Report",
            original_filename="user_b_billing.csv"
        )
        
        # Create billing records for user A
        self.record_a1 = BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_name="VM-A1",
            resource_id="ocid-vma1",
            compartment="Dev",
            region="us-ashburn-1",
            usage_start=datetime.datetime(2026, 1, 15, 10, 0, tzinfo=datetime.timezone.utc),
            cost=Decimal("150.00"),
            currency="USD"
        )
        self.record_a2 = BillingRecord.objects.create(
            upload=self.upload_a,
            service="Storage",
            resource_name="Bucket-A2",
            resource_id="ocid-bucketa2",
            compartment="Dev",
            region="us-ashburn-1",
            usage_start=datetime.datetime(2026, 1, 20, 10, 0, tzinfo=datetime.timezone.utc),
            cost=Decimal("50.00"),
            currency="USD"
        )
        self.record_a3 = BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_name="VM-A3",
            resource_id="ocid-vma3",
            compartment="Prod",
            region="us-phoenix-1",
            usage_start=datetime.datetime(2026, 2, 5, 12, 0, tzinfo=datetime.timezone.utc),
            cost=Decimal("300.00"),
            currency="USD"
        )
        
        # Create billing records for user B (Isolation test data)
        self.record_b1 = BillingRecord.objects.create(
            upload=self.upload_b,
            service="Database",
            resource_name="DB-B1",
            resource_id="ocid-dbb1",
            compartment="Prod",
            region="us-ashburn-1",
            usage_start=datetime.datetime(2026, 1, 15, 10, 0, tzinfo=datetime.timezone.utc),
            cost=Decimal("999.00"),
            currency="USD"
        )

    def test_dashboard_requires_authentication(self):
        """Test that the dashboard page redirects unauthenticated users to login."""
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_dashboard_user_data_isolation(self):
        """Test that user only sees their own billing data on the dashboard."""
        self.client.login(username="usera", password="password123")
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        
        # User A should see only their own costs aggregated (150 + 50 + 300 = 500)
        self.assertEqual(response.context["total_cost"], 500.00)
        
        # Verify that User B's database records are not present in User A's lists
        self.assertNotIn("Database", response.context["service_labels"])
        self.assertNotIn(999.00, response.context["service_data"])

    def test_total_cost_calculation(self):
        """Test correct overall cost summing logic."""
        self.client.login(username="usera", password="password123")
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.context["total_cost"], 500.00)

    def test_monthly_aggregation(self):
        """Test monthly cost aggregation works and returns correct formatted labels."""
        self.client.login(username="usera", password="password123")
        response = self.client.get(self.dashboard_url)
        
        # We have Jan 2026 (150 + 50 = 200) and Feb 2026 (300)
        labels = response.context["monthly_labels"]
        data = response.context["monthly_data"]
        
        self.assertEqual(labels, ["Jan 2026", "Feb 2026"])
        self.assertEqual(data, [200.00, 300.00])

    def test_service_aggregation(self):
        """Test service group aggregates."""
        self.client.login(username="usera", password="password123")
        response = self.client.get(self.dashboard_url)
        
        # Compute: 150 + 300 = 450, Storage: 50
        service_costs = response.context["service_costs"]
        compute_cost = next(item["total"] for item in service_costs if item["service"] == "Compute")
        storage_cost = next(item["total"] for item in service_costs if item["service"] == "Storage")
        
        self.assertEqual(compute_cost, 450.00)
        self.assertEqual(storage_cost, 50.00)

    def test_region_aggregation_and_percentage(self):
        """Test top regions costs and percentages relative to filtered total."""
        self.client.login(username="usera", password="password123")
        response = self.client.get(self.dashboard_url)
        
        # Total cost is 500. us-ashburn-1 spent 200 (40%), us-phoenix-1 spent 300 (60%)
        top_regions = response.context["top_regions"]
        ashburn = next(item for item in top_regions if item["region"] == "us-ashburn-1")
        phoenix = next(item for item in top_regions if item["region"] == "us-phoenix-1")
        
        self.assertEqual(ashburn["total"], 200.00)
        self.assertEqual(ashburn["percentage"], 40.00)
        self.assertEqual(phoenix["total"], 300.00)
        self.assertEqual(phoenix["percentage"], 60.00)

    def test_top_expensive_resources(self):
        """Test resource aggregation and descending ordering."""
        self.client.login(username="usera", password="password123")
        response = self.client.get(self.dashboard_url)
        
        top_resources = response.context["top_resources"]
        
        # Most expensive should be VM-A3 (cost 300)
        self.assertEqual(top_resources[0]["resource_name"], "VM-A3")
        self.assertEqual(top_resources[0]["total"], 300.00)
        # Second should be VM-A1 (cost 150)
        self.assertEqual(top_resources[1]["resource_name"], "VM-A1")

    def test_date_filtering(self):
        """Test filtering the records by start_date and end_date."""
        self.client.login(username="usera", password="password123")
        
        # Filter for January only
        response = self.client.get(f"{self.dashboard_url}?start_date=2026-01-01&end_date=2026-01-31")
        self.assertEqual(response.context["total_cost"], 200.00)
        
        # Filter for February onwards
        response = self.client.get(f"{self.dashboard_url}?start_date=2026-02-01")
        self.assertEqual(response.context["total_cost"], 300.00)

    def test_service_filtering(self):
        """Test filtering stats by a single service."""
        self.client.login(username="usera", password="password123")
        
        response = self.client.get(f"{self.dashboard_url}?service=Storage")
        self.assertEqual(response.context["total_cost"], 50.00)
        self.assertEqual(response.context["total_resources"], 1)

    def test_region_filtering(self):
        """Test filtering stats by region."""
        self.client.login(username="usera", password="password123")
        
        response = self.client.get(f"{self.dashboard_url}?region=us-phoenix-1")
        self.assertEqual(response.context["total_cost"], 300.00)

    def test_empty_dataset_handling(self):
        """Test response behavior when no billing data exists for the user."""
        # Create a new user with zero records
        user_c = User.objects.create_user(username="userc", email="c@example.com", password="password123")
        self.client.login(username="userc", password="password123")
        
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["has_any_data"])
        self.assertContains(response, "No billing data available. Upload an OCI billing report to begin.")

    def test_blank_resource_ids_excluded(self):
        """Verify empty and null resource IDs are not counted as unique resources."""
        self.client.login(username="usera", password="password123")
        
        # Create records with blank or null resource IDs (null is mapped to blank in char fields usually)
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_name="Blank-Resource-1",
            resource_id="",
            compartment="Dev",
            region="us-ashburn-1",
            usage_start=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
            cost=Decimal("10.00"),
            currency="USD"
        )
        
        response = self.client.get(self.dashboard_url)
        # Previous count was 3 (VM-A1, Bucket-A2, VM-A3). The new record has resource_id="" and should be excluded.
        self.assertEqual(response.context["total_resources"], 3)

    def test_unknown_resource_handling_and_fallbacks(self):
        """Verify grouping identities and display names for records with missing IDs or names."""
        self.client.login(username="usera", password="password123")
        
        # 1. resource_name exists but ID missing (should group by name, display name is name)
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_name="Only-Name-Resource",
            resource_id="",
            compartment="Dev",
            region="us-ashburn-1",
            usage_start=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
            cost=Decimal("400.00"),
            currency="USD"
        )
        
        # 2. resource_id exists but name missing (should group by id, display name is id)
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_name="",
            resource_id="only-id-resource",
            compartment="Dev",
            region="us-ashburn-1",
            usage_start=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
            cost=Decimal("450.00"),
            currency="USD"
        )
        
        # 3. Both resource_name and ID missing (should group as Unknown Resource, display name "Unknown Resource")
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_name="",
            resource_id="",
            compartment="Dev",
            region="us-ashburn-1",
            usage_start=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
            cost=Decimal("500.00"),
            currency="USD"
        )
        
        response = self.client.get(self.dashboard_url)
        top_resources = response.context["top_resources"]
        
        # Verify total cost of User A is 500 (from setup) + 400 + 450 + 500 = 1850.00
        self.assertEqual(response.context["total_cost"], 1850.00)
        
        # Check that top_resources contains our resources sorted descending by total spent:
        # Rank 1: Both missing (total 500) -> display_name: "Unknown Resource"
        # Rank 2: Only ID exists (total 450) -> display_name: "only-id-resource"
        # Rank 3: Only Name exists (total 400) -> display_name: "Only-Name-Resource"
        self.assertEqual(top_resources[0]["display_name"], "Unknown Resource")
        self.assertEqual(top_resources[0]["total"], 500.00)
        
        self.assertEqual(top_resources[1]["display_name"], "only-id-resource")
        self.assertEqual(top_resources[1]["total"], 450.00)
        
        self.assertEqual(top_resources[2]["display_name"], "Only-Name-Resource")
        self.assertEqual(top_resources[2]["total"], 400.00)

    def test_empty_service_and_region_handling(self):
        """Test empty/null service and region values are grouped as Unknown Service/Region."""
        self.client.login(username="usera", password="password123")
        
        # Create records with empty strings for service and region
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="",
            resource_name="Blank-Metadata-1",
            resource_id="ocid-blank-metadata-1",
            compartment="Dev",
            region="",
            usage_start=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
            cost=Decimal("40.00"),
            currency="USD"
        )
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="  ",
            resource_name="Blank-Metadata-2",
            resource_id="ocid-blank-metadata-2",
            compartment="Dev",
            region="  ",
            usage_start=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
            cost=Decimal("60.00"),
            currency="USD"
        )
        
        response = self.client.get(self.dashboard_url)
        
        # Verify empty dropdown filters are excluded
        self.assertNotIn("", response.context["available_services"])
        self.assertNotIn("  ", response.context["available_services"])
        self.assertNotIn("", response.context["available_regions"])
        
        # Verify aggregates group under "Unknown Service" and "Unknown Region" (total = 40 + 60 = 100)
        service_costs = response.context["service_costs"]
        unknown_svc = next(item for item in service_costs if item["service"] == "Unknown Service")
        self.assertEqual(unknown_svc["total"], 100.00)
        
        region_costs = response.context["top_regions"]
        unknown_reg = next(item for item in region_costs if item["region"] == "Unknown Region")
        self.assertEqual(unknown_reg["total"], 100.00)

    def test_invalid_date_input_graceful(self):
        """Verify invalid date formatting fails gracefully and date filters are bypassed."""
        self.client.login(username="usera", password="password123")
        
        # 1. Malformed start_date, valid end_date
        response = self.client.get(f"{self.dashboard_url}?start_date=invalid-date&end_date=2026-02-05")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_cost"], 500.00)
        self.assertEqual(response.context["date_warning"], "Invalid date format. Please select a valid date.")
        self.assertContains(response, "Invalid date format. Please select a valid date.")
        
        # 2. Valid start_date, malformed end_date
        response = self.client.get(f"{self.dashboard_url}?start_date=2026-01-20&end_date=invalid-date")
        self.assertEqual(response.status_code, 200)
        # Filtered by start_date only (costs >= 2026-01-20: Bucket-A2 50 + VM-A3 300 = 350)
        self.assertEqual(response.context["total_cost"], 350.00)
        self.assertEqual(response.context["date_warning"], "Invalid date format. Please select a valid date.")
        
        # 3. Both malformed
        response = self.client.get(f"{self.dashboard_url}?start_date=invalid-start&end_date=invalid-end")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_cost"], 500.00)
        self.assertEqual(response.context["date_warning"], "Invalid date format. Please select a valid date.")

    def test_start_date_later_than_end_date(self):
        """Verify date warning message and date filter bypass when start_date is after end_date."""
        self.client.login(username="usera", password="password123")
        
        response = self.client.get(f"{self.dashboard_url}?start_date=2026-02-01&end_date=2026-01-01")
        self.assertEqual(response.status_code, 200)
        # Bypasses date filter entirely, returning overall total cost 500
        self.assertEqual(response.context["total_cost"], 500.00)
        # Displays warning block
        self.assertEqual(response.context["date_warning"], "Start date cannot be later than end date.")
        self.assertContains(response, "Start date cannot be later than end date.")

    def test_single_currency_dataset(self):
        """Verify currency display for single currency records."""
        self.client.login(username="usera", password="password123")
        response = self.client.get(self.dashboard_url)
        
        self.assertEqual(response.context["currency"], "USD")
        self.assertFalse(response.context["has_multiple_currencies"])

    def test_multiple_currency_dataset(self):
        """Verify currency display warning when multiple currencies are detected in active dataset."""
        self.client.login(username="usera", password="password123")
        
        # Create a record with a different currency
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_name="VM-A4",
            resource_id="ocid-vma4",
            compartment="Dev",
            region="us-ashburn-1",
            usage_start=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
            cost=Decimal("100.00"),
            currency="EUR"
        )
        
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.context["currency"], "MULTI")
        self.assertTrue(response.context["has_multiple_currencies"])
        self.assertContains(response, "Multiple currencies detected. Combined monetary totals may not represent a directly comparable value.")

    def test_chart_data_containing_special_characters(self):
        """Verify Chart.js labels serialization works safely with quotes and other special characters."""
        self.client.login(username="usera", password="password123")
        
        # Create a record with quotes and special characters in service and region name
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Database's \"Premium\" Service & <HTML>",
            resource_name="Special-Char-VM",
            resource_id="ocid-special-vm",
            compartment="Dev",
            region="Ashburn & Phoenix (US)\"",
            usage_start=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
            cost=Decimal("100.00"),
            currency="USD"
        )
        
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Database&#x27;s &quot;Premium&quot; Service &amp; &lt;HTML&gt;")
        self.assertContains(response, "Ashburn &amp; Phoenix (US)&quot;")
        self.assertContains(response, "Database's")
