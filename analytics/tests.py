from decimal import Decimal
import datetime
from django.utils import timezone
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from billing.models import BillingUpload, BillingRecord
from analytics.models import CostAnomaly, WasteFinding
from analytics.services.anomaly_detector import (
    run_anomaly_detection_for_user,
    classify_severity,
    calculate_stats
)
from analytics.services.waste_detector import run_waste_detection_for_user

User = get_user_model()

class AnomalyDetectionTests(TestCase):
    def setUp(self):
        # Create users
        self.user_a = User.objects.create_user(username="usera", email="a@example.com", password="password123")
        self.user_b = User.objects.create_user(username="userb", email="b@example.com", password="password123")
        
        # Create billing uploads
        self.upload_a = BillingUpload.objects.create(
            uploaded_by=self.user_a,
            upload_type="Billing Report",
            original_filename="user_a_billing.csv"
        )
        self.upload_b = BillingUpload.objects.create(
            uploaded_by=self.user_b,
            upload_type="Billing Report",
            original_filename="user_b_billing.csv"
        )
        
        # Url targets
        self.list_url = reverse("anomaly-list")
        self.trigger_url = reverse("anomaly-trigger")

    def test_authentication_required(self):
        """Verify accessing views redirects to login."""
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 302)
        
        response = self.client.post(self.trigger_url)
        self.assertEqual(response.status_code, 302)

    def test_user_data_isolation(self):
        """Verify User A cannot see User B's anomalies."""
        # Create anomaly for user B
        anomaly_b = CostAnomaly.objects.create(
            user=self.user_b,
            billing_upload=self.upload_b,
            anomaly_type="DAILY_SPIKE",
            detected_date=datetime.date(2026, 1, 15),
            actual_cost=Decimal("200.00"),
            expected_cost=Decimal("50.00"),
            deviation_percentage=Decimal("300.00"),
            z_score=2.5,
            severity="HIGH",
            status="OPEN"
        )
        
        self.client.login(username="usera", password="password123")
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(anomaly_b, response.context["anomalies"])
        
        # Try to view details
        detail_url = reverse("anomaly-detail", kwargs={"pk": anomaly_b.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 403)
        
        # Try to edit status
        status_url = reverse("anomaly-update-status", kwargs={"pk": anomaly_b.pk})
        response = self.client.post(status_url, {"status": "REVIEWED"})
        self.assertEqual(response.status_code, 403)

    def test_no_billing_data(self):
        """Verify detection is graceful with no billing data."""
        results = run_anomaly_detection_for_user(self.user_a)
        self.assertEqual(results["created"], 0)
        self.assertIn("Insufficient data", results["message"])

    def test_insufficient_historical_data_fewer_than_7_prior_days(self):
        """Verify less than 7 historical days does not trigger anomaly evaluations."""
        # Create only 5 days of billing records
        for i in range(5):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-vma1",
                resource_name="VM-A1",
                compartment="Dev",
                region="us-ashburn-1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("20.00"),
                currency="USD"
            )
            
        results = run_anomaly_detection_for_user(self.user_a)
        self.assertEqual(results["created"], 0)
        self.assertEqual(CostAnomaly.objects.filter(user=self.user_a).count(), 0)

    def test_exactly_7_prior_observations_allows_evaluation(self):
        """Verify detection evaluates when exactly 7 prior dates are available for the baseline."""
        # Preceding 7 days: stable spend of 20
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-vma1",
                resource_name="VM-A1",
                compartment="Dev",
                region="us-ashburn-1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("20.00"),
                currency="USD"
            )
        # Day 8: Spike to 200
        spike_day = datetime.date(2026, 1, 8)
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_id="ocid-vma1",
            resource_name="VM-A1",
            compartment="Dev",
            region="us-ashburn-1",
            usage_start=datetime.datetime.combine(spike_day, datetime.time(10, 0)),
            cost=Decimal("200.00"),
            currency="USD"
        )
        
        results = run_anomaly_detection_for_user(self.user_a)
        # There should be anomalies created (Daily spike + unusual growth + service spike + resource spike)
        self.assertGreater(results["created"], 0)
        self.assertTrue(CostAnomaly.objects.filter(user=self.user_a, anomaly_type="DAILY_SPIKE", detected_date=spike_day).exists())

    def test_look_ahead_bias_prevention(self):
        """Verify future dates are excluded from past baseline evaluations."""
        # Setup 7 days of $20 spend
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-vma1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("20.00")
            )
            
        # Day 8: $20 spend (Normal)
        day_8 = datetime.date(2026, 1, 8)
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_id="ocid-vma1",
            usage_start=datetime.datetime.combine(day_8, datetime.time(10, 0)),
            cost=Decimal("20.00")
        )
        
        # Day 9: Massive Spike of $2000.00 (Future data relative to Day 8)
        day_9 = datetime.date(2026, 1, 9)
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_id="ocid-vma1",
            usage_start=datetime.datetime.combine(day_9, datetime.time(10, 0)),
            cost=Decimal("2000.00")
        )
        
        # Run detection
        run_anomaly_detection_for_user(self.user_a)
        
        # Verify Day 8 did NOT flag as daily anomaly (since its baseline cost mean is 20 and std_dev is 0,
        # and current value is 20, which is normal).
        # If look-ahead bias existed and Day 9's 2000 was included, Day 8 might have been affected,
        # or we might have evaluated Day 9's spike back onto Day 8.
        self.assertFalse(CostAnomaly.objects.filter(detected_date=day_8, anomaly_type="DAILY_SPIKE").exists())
        # Day 9 should be flagged as anomaly
        self.assertTrue(CostAnomaly.objects.filter(detected_date=day_9, anomaly_type="DAILY_SPIKE").exists())

    def test_normal_spending_does_not_flag(self):
        """Verify normal cost variations do not trigger alerts."""
        # 10 days of stable/varying spending around $50 (e.g. 48, 52, 50, etc)
        costs = [Decimal("50.00"), Decimal("52.00"), Decimal("48.00"), Decimal("51.00"), Decimal("49.00"),
                 Decimal("50.00"), Decimal("50.00"), Decimal("51.00"), Decimal("49.00"), Decimal("50.00")]
                 
        for i, val in enumerate(costs):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-vma1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=val
            )
            
        results = run_anomaly_detection_for_user(self.user_a)
        # Should detect no spikes
        daily_anomalies = CostAnomaly.objects.filter(user=self.user_a, anomaly_type="DAILY_SPIKE")
        self.assertEqual(daily_anomalies.count(), 0)

    def test_zero_variance_boundary_conditions(self):
        """Verify zero variance standard deviation conditions behave correctly."""
        # Preceding 7 days spend is exactly 20 (std dev = 0)
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-vma1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("20.00")
            )
            
        # Day 8 scenario A: Normal value ($20.00). Should not trigger anomaly.
        day_8_normal = datetime.date(2026, 1, 8)
        record_8 = BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_id="ocid-vma1",
            usage_start=datetime.datetime.combine(day_8_normal, datetime.time(10, 0)),
            cost=Decimal("20.00")
        )
        
        run_anomaly_detection_for_user(self.user_a)
        self.assertFalse(CostAnomaly.objects.filter(detected_date=day_8_normal, anomaly_type="DAILY_SPIKE").exists())
        
        # Day 8 scenario B: Large cost spike ($200.00). Should flag and set z_score to None.
        record_8.cost = Decimal("200.00")
        record_8.save()
        
        # Clean up database anomalies list to rerun fresh detection
        CostAnomaly.objects.filter(user=self.user_a).delete()
        
        run_anomaly_detection_for_user(self.user_a)
        anomaly = CostAnomaly.objects.get(detected_date=day_8_normal, anomaly_type="DAILY_SPIKE")
        self.assertEqual(anomaly.actual_cost, Decimal("200.00"))
        self.assertEqual(anomaly.expected_cost, Decimal("20.00"))
        self.assertIsNone(anomaly.z_score)

    def test_resource_identity_grouping_and_skipping(self):
        """Verify resource identity fallbacks and skipping when both fields are missing."""
        # Preceding 7 days
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            # Record 1: Name exists but ID missing (valid grouping)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="",
                resource_name="NameOnlyResource",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00")
            )
            # Record 2: ID exists but name missing (valid grouping)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Storage",
                resource_id="ocid-storage-1",
                resource_name="",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00")
            )
            # Record 3: Both missing (should aggregate into daily/service, but skip resource spike)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="",
                resource_name="",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00")
            )
            
        # Day 8: Spike everything
        day_8 = datetime.date(2026, 1, 8)
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_id="",
            resource_name="NameOnlyResource",
            usage_start=datetime.datetime.combine(day_8, datetime.time(10, 0)),
            cost=Decimal("100.00")
        )
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Storage",
            resource_id="ocid-storage-1",
            resource_name="",
            usage_start=datetime.datetime.combine(day_8, datetime.time(10, 0)),
            cost=Decimal("100.00")
        )
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_id="",
            resource_name="",
            usage_start=datetime.datetime.combine(day_8, datetime.time(10, 0)),
            cost=Decimal("100.00")
        )
        
        run_anomaly_detection_for_user(self.user_a)
        
        # Verify NameOnlyResource spike was created
        self.assertTrue(CostAnomaly.objects.filter(anomaly_type="RESOURCE_SPIKE", resource_name="NameOnlyResource").exists())
        # Verify ocid-storage-1 spike was created
        self.assertTrue(CostAnomaly.objects.filter(anomaly_type="RESOURCE_SPIKE", resource_id="ocid-storage-1").exists())
        # Verify no RESOURCE_SPIKE was created for the empty resource
        self.assertFalse(CostAnomaly.objects.filter(anomaly_type="RESOURCE_SPIKE", resource_id="", resource_name="").exists())

    def test_insignificant_growth_ignored(self):
        """Verify high growth percentage with low absolute cost increase is ignored."""
        # 7 days of $0.10 spend
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("0.10")
            )
            
        # Day 8: $1.00 spend.
        # Growth is (1.00 - 0.10)/0.10 * 100 = 900% (High pct, but absolute impact is 0.90 which is < MIN_GROWTH_ABSOLUTE_INCREASE)
        day_8 = datetime.date(2026, 1, 8)
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            usage_start=datetime.datetime.combine(day_8, datetime.time(10, 0)),
            cost=Decimal("1.00")
        )
        
        run_anomaly_detection_for_user(self.user_a)
        self.assertFalse(CostAnomaly.objects.filter(anomaly_type="UNUSUAL_GROWTH", detected_date=day_8).exists())

    def test_detection_idempotency_and_uniqueness(self):
        """Verify running detection repeatedly updates open status or skips dismissed ones."""
        # Setup 7 days of stable spend
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-vm-1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("20.00")
            )
        # Day 8 spike
        day_8 = datetime.date(2026, 1, 8)
        record_8 = BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_id="ocid-vm-1",
            usage_start=datetime.datetime.combine(day_8, datetime.time(10, 0)),
            cost=Decimal("200.00")
        )
        
        # 1st run: creates new
        results_1 = run_anomaly_detection_for_user(self.user_a)
        self.assertEqual(results_1["created"], 4) # (daily, service, resource, growth)
        
        # 2nd run: updates them (no new created, only updated)
        results_2 = run_anomaly_detection_for_user(self.user_a)
        self.assertEqual(results_2["created"], 0)
        self.assertEqual(results_2["updated"], 4)
        
        # Dismiss one anomaly and run again: should be skipped
        anomaly = CostAnomaly.objects.filter(user=self.user_a, anomaly_type="DAILY_SPIKE").first()
        anomaly.status = "DISMISSED"
        anomaly.save()
        
        results_3 = run_anomaly_detection_for_user(self.user_a)
        self.assertEqual(results_3["created"], 0)
        # 3 anomalies still OPEN should be updated, 1 DISMISSED should be skipped
        self.assertEqual(results_3["updated"], 3)
        self.assertEqual(results_3["skipped"], 1)

    def test_critical_dashboard_counter_excludes_high(self):
        """Verify dashboard critical counter strictly includes CRITICAL and excludes HIGH anomalies."""
        # Create an OPEN CRITICAL anomaly
        CostAnomaly.objects.create(
            user=self.user_a,
            anomaly_type="DAILY_SPIKE",
            detected_date=datetime.date(2026, 1, 15),
            actual_cost=Decimal("600.00"),
            expected_cost=Decimal("50.00"),
            deviation_percentage=Decimal("1100.00"),
            severity="CRITICAL",
            status="OPEN"
        )
        # Create an OPEN HIGH anomaly
        CostAnomaly.objects.create(
            user=self.user_a,
            anomaly_type="SERVICE_SPIKE",
            detected_date=datetime.date(2026, 1, 15),
            service_name="Compute",
            actual_cost=Decimal("200.00"),
            expected_cost=Decimal("50.00"),
            deviation_percentage=Decimal("300.00"),
            severity="HIGH",
            status="OPEN"
        )
        # Create a DISMISSED CRITICAL anomaly
        CostAnomaly.objects.create(
            user=self.user_a,
            anomaly_type="RESOURCE_SPIKE",
            detected_date=datetime.date(2026, 1, 15),
            actual_cost=Decimal("700.00"),
            expected_cost=Decimal("50.00"),
            deviation_percentage=Decimal("1300.00"),
            severity="CRITICAL",
            status="DISMISSED"
        )
        
        self.client.login(username="usera", password="password123")
        dashboard_url = reverse("dashboard-home")
        response = self.client.get(dashboard_url)
        self.assertEqual(response.status_code, 200)
        
        # Verify context counters:
        # open_anomalies_count = 2 (DAILY_SPIKE + SERVICE_SPIKE)
        # critical_anomalies_count = 1 (DAILY_SPIKE only - open & critical)
        self.assertEqual(response.context["open_anomalies_count"], 2)
        self.assertEqual(response.context["critical_anomalies_count"], 1)

    def test_status_transition_workflow(self):
        """Verify transitioning anomaly statuses via POST views."""
        anomaly = CostAnomaly.objects.create(
            user=self.user_a,
            anomaly_type="DAILY_SPIKE",
            detected_date=datetime.date(2026, 1, 15),
            actual_cost=Decimal("200.00"),
            expected_cost=Decimal("50.00"),
            deviation_percentage=Decimal("300.00"),
            severity="HIGH",
            status="OPEN"
        )
        
        self.client.login(username="usera", password="password123")
        status_url = reverse("anomaly-update-status", kwargs={"pk": anomaly.pk})
        
        # Transition to REVIEWED
        response = self.client.post(status_url, {"status": "REVIEWED"})
        self.assertEqual(response.status_code, 302)
        anomaly.refresh_from_db()
        self.assertEqual(anomaly.status, "REVIEWED")
        
        # Transition to DISMISSED
        response = self.client.post(status_url, {"status": "DISMISSED"})
        self.assertEqual(response.status_code, 302)
        anomaly.refresh_from_db()
        self.assertEqual(anomaly.status, "DISMISSED")

    def test_anomaly_filtering(self):
        """Verify list view supports GET parameter filtering."""
        # Create different type/status anomalies
        CostAnomaly.objects.create(
            user=self.user_a,
            anomaly_type="DAILY_SPIKE",
            detected_date=datetime.date(2026, 1, 15),
            actual_cost=Decimal("200.00"),
            expected_cost=Decimal("50.00"),
            deviation_percentage=Decimal("300.00"),
            severity="HIGH",
            status="OPEN"
        )
        CostAnomaly.objects.create(
            user=self.user_a,
            anomaly_type="SERVICE_SPIKE",
            detected_date=datetime.date(2026, 1, 16),
            actual_cost=Decimal("150.00"),
            expected_cost=Decimal("50.00"),
            deviation_percentage=Decimal("200.00"),
            severity="MEDIUM",
            status="REVIEWED"
        )
        
        self.client.login(username="usera", password="password123")
        
        # Filter status=OPEN
        response = self.client.get(f"{self.list_url}?status=OPEN")
        self.assertEqual(len(response.context["anomalies"]), 1)
        self.assertEqual(response.context["anomalies"][0].anomaly_type, "DAILY_SPIKE")
        
        # Filter severity=MEDIUM
        response = self.client.get(f"{self.list_url}?severity=MEDIUM")
        self.assertEqual(len(response.context["anomalies"]), 1)
        self.assertEqual(response.context["anomalies"][0].anomaly_type, "SERVICE_SPIKE")

    def test_unusual_growth_fewer_than_7_prior_dates_cannot_create_anomaly(self):
        """Verify that growth spike on a date with fewer than 7 prior dates does not create UNUSUAL_GROWTH."""
        # 9 days of data.
        # Day 1-5: 20
        # Day 6: 200 (spike) - has only 5 prior dates
        # Day 7-9: 20
        for i in range(9):
            day = datetime.date(2026, 1, i + 1)
            cost = Decimal("200.00") if i == 5 else Decimal("20.00")
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=cost
            )
            
        run_anomaly_detection_for_user(self.user_a)
        
        # Verify no UNUSUAL_GROWTH anomaly was created for Day 6 (index 5)
        day_6 = datetime.date(2026, 1, 6)
        self.assertFalse(CostAnomaly.objects.filter(detected_date=day_6, anomaly_type="UNUSUAL_GROWTH").exists())

    def test_unusual_growth_exactly_7_prior_dates_allows_evaluation(self):
        """Verify that growth spike on Day 8 (exactly 7 prior dates) is evaluated and can create UNUSUAL_GROWTH."""
        # Day 1-7: 20
        # Day 8: 200 (spike) - has exactly 7 prior dates
        # Day 9: 200
        for i in range(9):
            day = datetime.date(2026, 1, i + 1)
            cost = Decimal("200.00") if i >= 7 else Decimal("20.00")
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=cost
            )
            
        run_anomaly_detection_for_user(self.user_a)
        
        # Day 8 (index 7) has exactly 7 prior dates (indices 0 to 6)
        day_8 = datetime.date(2026, 1, 8)
        self.assertTrue(CostAnomaly.objects.filter(detected_date=day_8, anomaly_type="UNUSUAL_GROWTH").exists())
        
        # Day 8 compares against Day 7: previous_cost = 20.00, current_cost = 200.00
        anomaly = CostAnomaly.objects.get(detected_date=day_8, anomaly_type="UNUSUAL_GROWTH")
        self.assertEqual(anomaly.actual_cost, Decimal("200.00"))
        self.assertEqual(anomaly.expected_cost, Decimal("20.00")) # compares to Day 7

    def test_decimal_monetary_severity_calculations(self):
        """Verify that classify_severity handles Decimal values correctly and yields appropriate severity levels."""
        # Test CRITICAL severity with exact Decimals
        # Requires: cost_impact >= 500.00, z_score >= 3.0 or deviation_pct >= 200.0
        sev_critical = classify_severity(z_score=3.0, deviation_pct=Decimal("200.0"), cost_impact=Decimal("500.00"))
        self.assertEqual(sev_critical, "CRITICAL")
        
        # Test HIGH severity with exact Decimals
        # Requires: (cost_impact >= 100.00 and (z_score >= 2.5 or deviation_pct >= 100.0)) or cost_impact >= 500.00
        # Let's test cost_impact >= 500.00 with low stats
        sev_high_1 = classify_severity(z_score=1.0, deviation_pct=Decimal("10.0"), cost_impact=Decimal("500.00"))
        self.assertEqual(sev_high_1, "HIGH")
        
        # Let's test cost_impact >= 100.00 with z_score >= 2.5
        sev_high_2 = classify_severity(z_score=2.5, deviation_pct=Decimal("10.0"), cost_impact=Decimal("100.00"))
        self.assertEqual(sev_high_2, "HIGH")
        
        # Test MEDIUM severity with exact Decimals
        # Requires: (cost_impact >= 30.00 and (z_score >= 2.0 or deviation_pct >= 50.0)) or cost_impact >= 100.00
        # Let's test cost_impact >= 100.00 with low stats
        sev_medium_1 = classify_severity(z_score=1.0, deviation_pct=Decimal("10.0"), cost_impact=Decimal("100.00"))
        self.assertEqual(sev_medium_1, "MEDIUM")
        
        # Let's test cost_impact >= 30.00 with z_score >= 2.0
        sev_medium_2 = classify_severity(z_score=2.0, deviation_pct=Decimal("10.0"), cost_impact=Decimal("30.00"))
        self.assertEqual(sev_medium_2, "MEDIUM")
        
        # Test LOW severity with exact Decimals
        sev_low = classify_severity(z_score=1.0, deviation_pct=Decimal("10.0"), cost_impact=Decimal("29.99"))
        self.assertEqual(sev_low, "LOW")


class WasteDetectionTests(TestCase):
    def setUp(self):
        # Create users
        self.user_a = User.objects.create_user(username="usera_w", email="a_w@example.com", password="password123")
        self.user_b = User.objects.create_user(username="userb_w", email="b_w@example.com", password="password123")
        
        # Create billing uploads
        self.upload_a = BillingUpload.objects.create(
            uploaded_by=self.user_a,
            upload_type="Billing Report",
            original_filename="user_a_billing.csv"
        )
        self.upload_b = BillingUpload.objects.create(
            uploaded_by=self.user_b,
            upload_type="Billing Report",
            original_filename="user_b_billing.csv"
        )
        
        self.list_url = reverse("waste-list")
        self.trigger_url = reverse("waste-trigger")

    def test_authentication_required(self):
        """Verify accessing waste views redirects to login."""
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 302)
        
        response = self.client.post(self.trigger_url)
        self.assertEqual(response.status_code, 302)

    def test_user_isolation(self):
        """Verify User A cannot view User B's waste findings."""
        finding_b = WasteFinding.objects.create(
            user=self.user_b,
            waste_type="PERSISTENT_LOW_COST_RESOURCE",
            resource_key="id:ocid-b",
            resource_id="ocid-b",
            service_name="Compute",
            first_seen=datetime.date(2026, 1, 1),
            last_seen=datetime.date(2026, 1, 10),
            observation_days=10,
            calendar_span_days=10,
            coverage_ratio=Decimal("1.0"),
            total_cost=Decimal("15.00"),
            average_daily_cost=Decimal("1.50"),
            estimated_monthly_cost=Decimal("45.00"),
            estimated_monthly_savings=Decimal("22.50"),
            confidence="MEDIUM",
            evidence="Evidence",
            status="OPEN"
        )
        
        self.client.login(username="usera_w", password="password123")
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(finding_b, response.context["findings"])
        
        detail_url = reverse("waste-detail", kwargs={"pk": finding_b.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 403)
        
        status_url = reverse("waste-update-status", kwargs={"pk": finding_b.pk})
        response = self.client.post(status_url, {"status": "REVIEWED"})
        self.assertEqual(response.status_code, 403)

    def test_no_billing_records_produces_no_findings(self):
        """Verify running detection on empty billing records returns gracefully."""
        results = run_waste_detection_for_user(self.user_a)
        self.assertEqual(results["created"], 0)
        self.assertEqual(results["analyzed"], 0)

    def test_insufficient_observation_window_produces_no_findings(self):
        """Verify resources with fewer than 7 observed dates are skipped."""
        # Create 5 days of billing records
        for i in range(5):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00")
            )
        results = run_waste_detection_for_user(self.user_a)
        self.assertEqual(results["created"], 0)

    def test_insufficient_coverage_ratio_produces_no_findings(self):
        """Verify resources with less than 70% coverage ratio are skipped."""
        # Span: 1 to 15 (15 days), but only seen on 6 dates (coverage = 6/15 = 40%)
        dates = [1, 2, 3, 13, 14, 15]
        for d in dates:
            day = datetime.date(2026, 1, d)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00")
            )
        results = run_waste_detection_for_user(self.user_a)
        self.assertEqual(results["created"], 0)

    def test_resource_identity_and_fallback(self):
        """Verify resource key is generated from resource_id first, falling back to resource_name."""
        # Case A: Resource ID exists
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-1",
                resource_name="Instance-1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("1.00")
            )
        
        # Case B: Only Resource Name exists (fallback)
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="",
                resource_name="NameOnlyInstance",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("1.00")
            )

        run_waste_detection_for_user(self.user_a)
        
        # Verify findings exist with correct keys
        self.assertTrue(WasteFinding.objects.filter(resource_key="id:ocid-1").exists())
        self.assertTrue(WasteFinding.objects.filter(resource_key="name:NameOnlyInstance").exists())

    def test_missing_resource_identity_skipped(self):
        """Verify records missing both resource ID and name are skipped."""
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="",
                resource_name="",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("1.00")
            )
        results = run_waste_detection_for_user(self.user_a)
        self.assertEqual(results["created"], 0)

    def test_same_resource_id_with_changed_resource_name_updates_finding(self):
        """Verify updating a resource name for the same resource_id refreshes the name without creating duplicate."""
        # 1st run: name is "OldName"
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-1",
                resource_name="OldName",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("1.00")
            )
        run_waste_detection_for_user(self.user_a)
        self.assertEqual(WasteFinding.objects.filter(resource_key="id:ocid-1", waste_type="PERSISTENT_LOW_COST_RESOURCE").count(), 1)
        self.assertEqual(WasteFinding.objects.get(resource_key="id:ocid-1", waste_type="PERSISTENT_LOW_COST_RESOURCE").resource_name, "OldName")
        
        # 2nd run: name is "NewName"
        for i in range(7):
            day = datetime.date(2026, 1, i + 8)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-1",
                resource_name="NewName",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("1.00")
            )
        run_waste_detection_for_user(self.user_a)
        # Verify it updated the existing finding rather than creating a new one
        self.assertEqual(WasteFinding.objects.filter(resource_key="id:ocid-1", waste_type="PERSISTENT_LOW_COST_RESOURCE").count(), 1)
        self.assertEqual(WasteFinding.objects.get(resource_key="id:ocid-1", waste_type="PERSISTENT_LOW_COST_RESOURCE").resource_name, "NewName")

    def test_different_currencies_remain_separate_findings(self):
        """Verify that records with different currencies create separate findings for the same resource ID."""
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            # USD
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("1.00"),
                currency="USD"
            )
            # EUR
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("1.00"),
                currency="EUR"
            )
            
        run_waste_detection_for_user(self.user_a)
        findings = WasteFinding.objects.filter(resource_key="id:ocid-1", waste_type="PERSISTENT_LOW_COST_RESOURCE")
        self.assertEqual(findings.count(), 2)
        self.assertTrue(findings.filter(currency="USD").exists())
        self.assertTrue(findings.filter(currency="EUR").exists())

    def test_unsupported_usage_units_skip_dormant_detection(self):
        """Verify that unsupported usage units skip dormant cost pattern detection."""
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("1.00"),
                usage_quantity=Decimal("0.00"),
                usage_unit="ArbitraryUnit" # Unsupported unit for Compute
            )
        run_waste_detection_for_user(self.user_a)
        # Dormant cost pattern should not be created (Persistent Low Cost might be created if thresholds match,
        # but let's confirm DORMANT_COST_PATTERN is absent)
        self.assertFalse(WasteFinding.objects.filter(waste_type="DORMANT_COST_PATTERN").exists())

    def test_inconsistent_usage_units_skip_dormant_detection(self):
        """Verify that inconsistent usage units for the same resource skip dormant detection."""
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            # Mix OCPU-Hours and GB-Hours
            unit = "OCPU-Hours" if i % 2 == 0 else "GB-Hours"
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("1.00"),
                usage_quantity=Decimal("0.00"),
                usage_unit=unit
            )
        run_waste_detection_for_user(self.user_a)
        self.assertFalse(WasteFinding.objects.filter(waste_type="DORMANT_COST_PATTERN").exists())

    def test_storage_heuristic_confidence_cap(self):
        """Verify POSSIBLE_UNUSED_STORAGE never exceeds MEDIUM confidence."""
        # 15 days of stable storage costs (variance = 0)
        for i in range(15):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Block Volume",
                resource_id="ocid-vol",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("1.00"),
                usage_unit="GB-Months",
                usage_quantity=Decimal("10.00")
            )
        run_waste_detection_for_user(self.user_a)
        
        # Verify finding created
        finding = WasteFinding.objects.get(waste_type="POSSIBLE_UNUSED_STORAGE")
        # Capped at MEDIUM confidence
        self.assertEqual(finding.confidence, "MEDIUM")
        # Verify evidence uses correct conservative language
        self.assertIn("Storage resource shows stable recurring billing with low or unchanged recorded usage under a validated billing unit.", finding.evidence)
        self.assertIn("potential optimization candidate", finding.evidence.lower())
        self.assertNotIn("unattached", finding.evidence.lower())

    def test_repeated_detection_remains_idempotent_and_updates(self):
        """Verify repeated waste runs update existing OPEN findings without creating duplicates."""
        for i in range(7):
            day = datetime.date(2026, 1, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-1",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("1.00")
            )
            
        results_1 = run_waste_detection_for_user(self.user_a)
        self.assertGreater(results_1["created"], 0)
        self.assertEqual(results_1["updated"], 0)
        
        # Check initial counts
        initial_count = WasteFinding.objects.filter(user=self.user_a).count()
        
        # Run again
        results_2 = run_waste_detection_for_user(self.user_a)
        self.assertEqual(results_2["created"], 0)
        self.assertGreater(results_2["updated"], 0)
        
        # Verify no duplicate findings were created
        self.assertEqual(WasteFinding.objects.filter(user=self.user_a).count(), initial_count)

    def test_status_transitions_and_ownership(self):
        """Verify status workflow status updates respect authentication and user isolation."""
        finding = WasteFinding.objects.create(
            user=self.user_a,
            waste_type="PERSISTENT_LOW_COST_RESOURCE",
            resource_key="id:ocid-1",
            resource_id="ocid-1",
            service_name="Compute",
            first_seen=datetime.date(2026, 1, 1),
            last_seen=datetime.date(2026, 1, 10),
            observation_days=10,
            calendar_span_days=10,
            coverage_ratio=Decimal("1.0"),
            total_cost=Decimal("15.00"),
            average_daily_cost=Decimal("1.50"),
            estimated_monthly_cost=Decimal("45.00"),
            estimated_monthly_savings=Decimal("22.50"),
            confidence="MEDIUM",
            evidence="Evidence",
            status="OPEN"
        )
        
        self.client.login(username="usera_w", password="password123")
        status_url = reverse("waste-update-status", kwargs={"pk": finding.pk})
        
        # Transition to REVIEWED
        response = self.client.post(status_url, {"status": "REVIEWED"})
        self.assertEqual(response.status_code, 302)
        finding.refresh_from_db()
        self.assertEqual(finding.status, "REVIEWED")
        
        # Transition to DISMISSED
        response = self.client.post(status_url, {"status": "DISMISSED"})
        self.assertEqual(response.status_code, 302)
        finding.refresh_from_db()
        self.assertEqual(finding.status, "DISMISSED")

    def test_dashboard_waste_counters_and_multi_currency(self):
        """Verify the dashboard page displays waste findings metrics and separates multi-currency saving values."""
        # Create open findings in different currencies
        WasteFinding.objects.create(
            user=self.user_a,
            waste_type="PERSISTENT_LOW_COST_RESOURCE",
            resource_key="id:ocid-usd",
            service_name="Compute",
            currency="USD",
            first_seen=datetime.date(2026, 1, 1),
            last_seen=datetime.date(2026, 1, 10),
            observation_days=10,
            calendar_span_days=10,
            coverage_ratio=Decimal("1.0"),
            total_cost=Decimal("15.00"),
            average_daily_cost=Decimal("1.50"),
            estimated_monthly_cost=Decimal("45.00"),
            estimated_monthly_savings=Decimal("20.00"),
            status="OPEN"
        )
        WasteFinding.objects.create(
            user=self.user_a,
            waste_type="PERSISTENT_LOW_COST_RESOURCE",
            resource_key="id:ocid-eur",
            service_name="Compute",
            currency="EUR",
            first_seen=datetime.date(2026, 1, 1),
            last_seen=datetime.date(2026, 1, 10),
            observation_days=10,
            calendar_span_days=10,
            coverage_ratio=Decimal("1.0"),
            total_cost=Decimal("15.00"),
            average_daily_cost=Decimal("1.50"),
            estimated_monthly_cost=Decimal("45.00"),
            estimated_monthly_savings=Decimal("30.00"),
            status="OPEN"
        )
        
        self.client.login(username="usera_w", password="password123")
        dashboard_url = reverse("dashboard-home")
        response = self.client.get(dashboard_url)
        self.assertEqual(response.status_code, 200)
        
        self.assertEqual(response.context["open_waste_count"], 2)
        self.assertIn("20.00 USD", response.context["potential_waste_savings"])
        self.assertIn("30.00 EUR", response.context["potential_waste_savings"])
        self.assertTrue(response.context["waste_has_multiple_currencies"])

    def test_stale_resource_cost_logic(self):
        """Verify STALE_RESOURCE_COST checks cost and usage stability correctly."""
        # 1. Stable cost + stable compatible usage -> finding allowed
        for i in range(15):
            day = datetime.date(2026, 2, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-stable",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00"),
                usage_unit="OCPU-Hours",
                usage_quantity=Decimal("5.00")
            )
            
        # 2. Stable cost + volatile compatible usage -> NO stale finding
        for i in range(15):
            day = datetime.date(2026, 2, i + 1)
            # cost is stable ($10.00), usage is volatile (varies between 5 and 50)
            usage_qty = Decimal("5.00") if i % 2 == 0 else Decimal("50.00")
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-volatile-usage",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00"),
                usage_unit="OCPU-Hours",
                usage_quantity=usage_qty
            )
            
        # 3. Stable cost + unsupported usage unit -> conservative finding allowed
        for i in range(15):
            day = datetime.date(2026, 2, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="ocid-unsupported-unit",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00"),
                usage_unit="UnsupportedUnit",
                usage_quantity=Decimal("5.00")
            )
            
        run_waste_detection_for_user(self.user_a)
        
        # Verify Case 1 finding exists (and is HIGH confidence)
        finding_stable = WasteFinding.objects.get(resource_key="id:ocid-stable", waste_type="STALE_RESOURCE_COST")
        self.assertEqual(finding_stable.confidence, "HIGH")
        self.assertNotIn("usage metrics unavailable/incompatible", finding_stable.evidence)
        
        # Verify Case 2: NO stale finding exists for ocid-volatile-usage
        self.assertFalse(WasteFinding.objects.filter(resource_key="id:ocid-volatile-usage", waste_type="STALE_RESOURCE_COST").exists())
        
        # Verify Case 3 finding exists (and is capped at MEDIUM confidence, with conservative evidence)
        finding_unsupported = WasteFinding.objects.get(resource_key="id:ocid-unsupported-unit", waste_type="STALE_RESOURCE_COST")
        self.assertEqual(finding_unsupported.confidence, "MEDIUM")
        self.assertIn("usage metrics unavailable/incompatible", finding_unsupported.evidence)

    def test_possible_unused_storage_logic(self):
        """Verify POSSIBLE_UNUSED_STORAGE checks storage service, unit compatibility, and variance stability."""
        # 1. Storage + stable cost + stable/low compatible usage -> finding allowed
        for i in range(15):
            day = datetime.date(2026, 3, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Block Volume",
                resource_id="ocid-storage-ok",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00"),
                usage_unit="GB-Months",
                usage_quantity=Decimal("10.00")
            )
            
        # 2. Storage + volatile usage -> no POSSIBLE_UNUSED_STORAGE finding
        for i in range(15):
            day = datetime.date(2026, 3, i + 1)
            usage_qty = Decimal("10.00") if i % 2 == 0 else Decimal("100.00")
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Block Volume",
                resource_id="ocid-storage-volatile-usage",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00"),
                usage_unit="GB-Months",
                usage_quantity=usage_qty
            )
            
        # 3. Storage + unsupported usage unit -> no POSSIBLE_UNUSED_STORAGE finding
        for i in range(15):
            day = datetime.date(2026, 3, i + 1)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Block Volume",
                resource_id="ocid-storage-unsupported-unit",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00"),
                usage_unit="UnsupportedUnit",
                usage_quantity=Decimal("10.00")
            )
            
        # 4. Storage + inconsistent usage units -> no POSSIBLE_UNUSED_STORAGE finding
        for i in range(15):
            day = datetime.date(2026, 3, i + 1)
            unit = "GB-Months" if i % 2 == 0 else "GB"
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Block Volume",
                resource_id="ocid-storage-inconsistent-units",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=Decimal("10.00"),
                usage_unit=unit,
                usage_quantity=Decimal("10.00")
            )
            
        # 5. Storage + volatile cost -> no POSSIBLE_UNUSED_STORAGE finding
        for i in range(15):
            day = datetime.date(2026, 3, i + 1)
            cost_val = Decimal("10.00") if i % 2 == 0 else Decimal("50.00")
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Block Volume",
                resource_id="ocid-storage-volatile-cost",
                usage_start=datetime.datetime.combine(day, datetime.time(10, 0)),
                cost=cost_val,
                usage_unit="GB-Months",
                usage_quantity=Decimal("10.00")
            )
            
        run_waste_detection_for_user(self.user_a)
        
        # Verify Case 1 finding exists and has confidence capped at MEDIUM
        finding_ok = WasteFinding.objects.get(resource_key="id:ocid-storage-ok", waste_type="POSSIBLE_UNUSED_STORAGE")
        self.assertEqual(finding_ok.confidence, "MEDIUM")
        self.assertIn("Storage resource shows stable recurring billing with low or unchanged recorded usage under a validated billing unit.", finding_ok.evidence)
        
        # Verify Case 2, 3, 4, 5 do not have POSSIBLE_UNUSED_STORAGE findings
        self.assertFalse(WasteFinding.objects.filter(resource_key="id:ocid-storage-volatile-usage", waste_type="POSSIBLE_UNUSED_STORAGE").exists())
        self.assertFalse(WasteFinding.objects.filter(resource_key="id:ocid-storage-unsupported-unit", waste_type="POSSIBLE_UNUSED_STORAGE").exists())
        self.assertFalse(WasteFinding.objects.filter(resource_key="id:ocid-storage-inconsistent-units", waste_type="POSSIBLE_UNUSED_STORAGE").exists())
        self.assertFalse(WasteFinding.objects.filter(resource_key="id:ocid-storage-volatile-cost", waste_type="POSSIBLE_UNUSED_STORAGE").exists())


class ForecastingTests(TestCase):
    def setUp(self):
        self.user_a = User.objects.create_user(username="forecastera", email="fa@example.com", password="password123")
        self.user_b = User.objects.create_user(username="forecasterb", email="fb@example.com", password="password123")
        
        self.upload_a = BillingUpload.objects.create(
            uploaded_by=self.user_a,
            upload_type="Billing Report",
            original_filename="user_a_forecast.csv"
        )
        self.upload_b = BillingUpload.objects.create(
            uploaded_by=self.user_b,
            upload_type="Billing Report",
            original_filename="user_b_forecast.csv"
        )
        
        self.today = timezone.localdate()
        self.current_month_index = self.today.year * 12 + self.today.month

    def _create_record(self, user, upload, months_offset, cost, currency="USD"):
        # offset is relative to current_month_index
        target_index = self.current_month_index + months_offset
        yr = (target_index - 1) // 12
        mn = (target_index - 1) % 12 + 1
        
        # Use middle day of month for safety
        dt = datetime.datetime(yr, mn, 15, 12, 0, tzinfo=datetime.timezone.utc)
        
        BillingRecord.objects.create(
            upload=upload,
            service="Compute",
            resource_id="ocid-vm-forecast",
            region="us-ashburn-1",
            cost=Decimal(str(cost)),
            currency=currency,
            usage_start=dt
        )

    def test_month_completeness_and_future_exclusion(self):
        # Current month: MONTH_TO_DATE (0 offset)
        self._create_record(self.user_a, self.upload_a, 0, 100)
        # Previous completed month (-1 offset)
        self._create_record(self.user_a, self.upload_a, -1, 150)
        # Future month (+1 offset)
        self._create_record(self.user_a, self.upload_a, 1, 200)
        
        from analytics.services.cost_forecaster import get_forecast_for_user
        res = get_forecast_for_user(self.user_a)
        
        usd_res = res.get("USD", {})
        self.assertTrue(usd_res.get("has_future_records"))
        
        # Verify current month is treated as MONTH_TO_DATE
        mtd = usd_res.get("current_month_mtd")
        self.assertIsNotNone(mtd)
        self.assertEqual(mtd["cost"], Decimal("100.00"))
        
        # Completed months should only contain the previous month (no future, no current)
        hist = usd_res.get("historical_months", [])
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["cost"], Decimal("150.00"))

    def test_missing_months_and_coverage(self):
        # Create completed months: -4, -2, -1 (gap at -3)
        self._create_record(self.user_a, self.upload_a, -4, 100)
        self._create_record(self.user_a, self.upload_a, -2, 120)
        self._create_record(self.user_a, self.upload_a, -1, 130)
        
        from analytics.services.cost_forecaster import get_forecast_for_user
        res = get_forecast_for_user(self.user_a)
        usd_res = res.get("USD", {})
        
        # Verify indices spacing preservation
        # Span = from index of -4 to index of -1, which is 4 months (e.g. -4, -3, -2, -1)
        self.assertEqual(usd_res["historical_month_count"], 3)
        self.assertEqual(usd_res["historical_span_months"], 4)
        self.assertEqual(usd_res["missing_month_count"], 1)
        self.assertEqual(usd_res["coverage_ratio"], Decimal("0.75"))
        # Since coverage is 0.75 (which is >= 0.75 but < 0.90), confidence can be at most MEDIUM
        # Since we have exactly 3 months, it must remain LOW confidence
        self.assertEqual(usd_res["confidence"], "LOW")

    def test_forecast_horizons_and_start_month(self):
        # Create 3 completed months
        self._create_record(self.user_a, self.upload_a, -3, 100)
        self._create_record(self.user_a, self.upload_a, -2, 100)
        self._create_record(self.user_a, self.upload_a, -1, 100)
        
        from analytics.services.cost_forecaster import get_forecast_for_user
        res = get_forecast_for_user(self.user_a)
        usd_res = res.get("USD", {})
        
        self.assertTrue(usd_res["forecast_available"])
        self.assertEqual(usd_res["next_month_forecast"], Decimal("100.00"))
        self.assertEqual(usd_res["three_month_forecast"], Decimal("300.00"))
        self.assertEqual(usd_res["six_month_forecast"], Decimal("600.00"))
        
        # Ensure predicted months are chronologically strictly after current month
        forecast_months = usd_res["forecast_months"]
        self.assertEqual(len(forecast_months), 6)
        
        next_month_target = self.current_month_index + 1
        y_next = (next_month_target - 1) // 12
        m_next = (next_month_target - 1) % 12 + 1
        self.assertEqual(forecast_months[0]["month"], f"{y_next:04d}-{m_next:02d}")

    def test_regression_trends(self):
        # 1. Increasing trend
        # x = 0 (100), x = 1 (110), x = 2 (120) -> slope is 10
        self._create_record(self.user_a, self.upload_a, -3, 100)
        self._create_record(self.user_a, self.upload_a, -2, 110)
        self._create_record(self.user_a, self.upload_a, -1, 120)
        
        from analytics.services.cost_forecaster import get_forecast_for_user
        res = get_forecast_for_user(self.user_a)
        usd_res = res.get("USD", {})
        
        # June cost is 120 (relative x = 2). Current month July is offset +0 (relative x = 3).
        # Predicted Aug (+1 offset, relative x = 4) should be: 100 + 10 * 4 = 140
        self.assertEqual(usd_res["next_month_forecast"], Decimal("140.00"))
        
        # Clear records
        BillingRecord.objects.all().delete()
        
        # 2. Decreasing trend with floor at zero
        # x = 0 (100), x = 1 (50), x = 2 (10) -> slope is -45
        # Aug (+1 offset, relative x = 4) prediction would be negative -> must be floored at 0.00
        self._create_record(self.user_a, self.upload_a, -3, 100)
        self._create_record(self.user_a, self.upload_a, -2, 50)
        self._create_record(self.user_a, self.upload_a, -1, 10)
        
        res = get_forecast_for_user(self.user_a)
        usd_res = res.get("USD", {})
        self.assertEqual(usd_res["next_month_forecast"], Decimal("0.00"))
        self.assertEqual(usd_res["three_month_forecast"], Decimal("0.00"))

    def test_confidence_mapping(self):
        # 1. 3 completed months -> LOW
        self._create_record(self.user_a, self.upload_a, -3, 100)
        self._create_record(self.user_a, self.upload_a, -2, 100)
        self._create_record(self.user_a, self.upload_a, -1, 100)
        
        from analytics.services.cost_forecaster import get_forecast_for_user
        res = get_forecast_for_user(self.user_a)
        self.assertEqual(res["USD"]["confidence"], "LOW")
        
        # 2. 6 completed months, stable -> MEDIUM
        BillingRecord.objects.all().delete()
        for i in range(1, 7):
            self._create_record(self.user_a, self.upload_a, -i, 100)
        res = get_forecast_for_user(self.user_a)
        self.assertEqual(res["USD"]["confidence"], "MEDIUM")
        
        # 3. 12 completed months, stable -> HIGH
        BillingRecord.objects.all().delete()
        for i in range(1, 13):
            self._create_record(self.user_a, self.upload_a, -i, 100)
        res = get_forecast_for_user(self.user_a)
        self.assertEqual(res["USD"]["confidence"], "HIGH")
        
        # 4. 12 completed months but volatile -> degraded
        BillingRecord.objects.all().delete()
        for i in range(1, 13):
            cost = 100 if i % 2 == 0 else 250
            self._create_record(self.user_a, self.upload_a, -i, cost)
        res = get_forecast_for_user(self.user_a)
        self.assertNotEqual(res["USD"]["confidence"], "HIGH")

        # 5. Flat zero history -> LOW
        BillingRecord.objects.all().delete()
        for i in range(1, 13):
            self._create_record(self.user_a, self.upload_a, -i, 0)
        res = get_forecast_for_user(self.user_a)
        self.assertEqual(res["USD"]["confidence"], "LOW")

    def test_forecast_range(self):
        # Less than 5 months -> bounds are None
        for i in range(1, 5):
            self._create_record(self.user_a, self.upload_a, -i, 100)
        from analytics.services.cost_forecaster import get_forecast_for_user
        res = get_forecast_for_user(self.user_a)
        self.assertIsNone(res["USD"]["forecast_months"][0]["lower_bound"])
        self.assertIsNone(res["USD"]["forecast_months"][0]["upper_bound"])
        
        # 5 or more completed months -> bounds are calculated via RMSE
        self._create_record(self.user_a, self.upload_a, -5, 120)
        res = get_forecast_for_user(self.user_a)
        self.assertIsNotNone(res["USD"]["forecast_months"][0]["lower_bound"])
        self.assertIsNotNone(res["USD"]["forecast_months"][0]["upper_bound"])
        self.assertTrue(res["USD"]["forecast_months"][0]["lower_bound"] >= Decimal("0.00"))

    def test_multicurrency_isolation(self):
        # Create 12 USD records (forecast available)
        for i in range(1, 13):
            self._create_record(self.user_a, self.upload_a, -i, 100, currency="USD")
        # Create 2 EUR records (forecast unavailable)
        for i in range(1, 3):
            self._create_record(self.user_a, self.upload_a, -i, 50, currency="EUR")
            
        from analytics.services.cost_forecaster import get_forecast_for_user
        res = get_forecast_for_user(self.user_a)
        
        self.assertTrue(res["USD"]["forecast_available"])
        self.assertFalse(res["EUR"]["forecast_available"])

    def test_security_user_isolation_and_login(self):
        # Create records for User B
        for i in range(1, 5):
            self._create_record(self.user_b, self.upload_b, -i, 100)
            
        # Run forecast on User A (who has no records)
        from analytics.services.cost_forecaster import get_forecast_for_user
        res_a = get_forecast_for_user(self.user_a)
        self.assertEqual(len(res_a), 0)
        
        # Verify login required for view
        url = reverse("cost-forecast")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        
        # Log in User B and test access
        self.client.login(username="forecasterb", password="password123")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("forecast_results", response.context)

    def test_calendar_boundaries(self):
        # Build month advance checks and leap year checks
        from analytics.services.cost_forecaster import get_forecast_for_user
        
        # Test offset boundary labels by formatting relative indexes
        self._create_record(self.user_a, self.upload_a, -3, 100)
        self._create_record(self.user_a, self.upload_a, -2, 100)
        self._create_record(self.user_a, self.upload_a, -1, 100)
        
        res = get_forecast_for_user(self.user_a)
        usd_res = res.get("USD", {})
        self.assertTrue(usd_res["forecast_available"])
        
        # Verify year transition and months labeling formatting
        forecast_months = usd_res["forecast_months"]
        for fm in forecast_months:
            m_str = fm["month"]
            self.assertEqual(len(m_str), 7)
            self.assertEqual(m_str[4], "-")
            yr_part, mn_part = map(int, m_str.split("-"))
            self.assertTrue(1 <= mn_part <= 12)
            self.assertTrue(yr_part >= 2026)

    def test_timezone_boundary_classification(self):
        from django.utils import timezone
        
        # Override timezone to GMT+5:30 (e.g. Asia/Kolkata)
        with timezone.override("Asia/Kolkata"):
            # A naive datetime (no exception should occur)
            naive_dt = datetime.datetime(2026, 6, 15, 12, 0)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="naive-test",
                cost=Decimal("10.00"),
                currency="USD",
                usage_start=naive_dt
            )
            
            # An aware datetime that is UTC: 2026-06-30 23:30:00+00:00
            # Under Asia/Kolkata timezone: 2026-07-01 05:00:00+05:30
            # So in local time, it belongs to July 2026.
            aware_dt = datetime.datetime(2026, 6, 30, 23, 30, tzinfo=datetime.timezone.utc)
            BillingRecord.objects.create(
                upload=self.upload_a,
                service="Compute",
                resource_id="aware-test",
                cost=Decimal("20.00"),
                currency="USD",
                usage_start=aware_dt
            )
            
            # Create another record to satisfy 3 months minimum in June, May, April
            self._create_record(self.user_a, self.upload_a, -3, 100) # April
            self._create_record(self.user_a, self.upload_a, -2, 100) # May
            
            from analytics.services.cost_forecaster import get_forecast_for_user
            res = get_forecast_for_user(self.user_a)
            usd_res = res.get("USD", {})
            
            # June has naive_dt (10.00) + May (-2 offset) has 100.00 + April (-3 offset) has 100.00 -> 3 completed months!
            # June total completed cost is 10.00.
            # July has aware_dt (20.00). July is the current month MTD.
            # Let's verify that July MTD has 20.00 and June completed has 10.00.
            mtd = usd_res.get("current_month_mtd")
            self.assertIsNotNone(mtd)
            self.assertEqual(mtd["month"], "2026-07")
            self.assertEqual(mtd["cost"], Decimal("20.00"))
            
            hist = usd_res.get("historical_months", [])
            june_hist = [h for h in hist if h["month"] == "2026-06"]
            self.assertEqual(len(june_hist), 1)
            self.assertEqual(june_hist[0]["cost"], Decimal("10.00"))

    def test_future_only_currency_and_empty_user(self):
        from analytics.services.cost_forecaster import get_forecast_for_user
        
        # E. Empty user
        res_empty = get_forecast_for_user(self.user_a)
        self.assertEqual(res_empty, {})
        
        # B. Future-only EUR record
        self._create_record(self.user_a, self.upload_a, 2, 100, currency="EUR")
        
        res = get_forecast_for_user(self.user_a)
        self.assertIn("EUR", res)
        self.assertNotIn("UNKNOWN", res)
        
        eur_res = res["EUR"]
        self.assertFalse(eur_res["forecast_available"])
        self.assertTrue(eur_res["has_future_records"])
        self.assertEqual(eur_res["historical_month_count"], 0)
        self.assertEqual(len(eur_res["forecast_months"]), 0)
        
        # C. Future-only multi-currency
        self._create_record(self.user_a, self.upload_a, 2, 150, currency="USD")
        
        res_multi = get_forecast_for_user(self.user_a)
        self.assertIn("EUR", res_multi)
        self.assertIn("USD", res_multi)
        
        self.assertFalse(res_multi["USD"]["forecast_available"])
        self.assertTrue(res_multi["USD"]["has_future_records"])
        self.assertEqual(res_multi["USD"]["historical_month_count"], 0)
        
        # D. Blank future currency -> UNKNOWN
        self._create_record(self.user_a, self.upload_a, 2, 80, currency="")
        
        res_blank = get_forecast_for_user(self.user_a)
        self.assertIn("UNKNOWN", res_blank)
        self.assertTrue(res_blank["UNKNOWN"]["has_future_records"])
        self.assertFalse(res_blank["UNKNOWN"]["forecast_available"])


class SimulatorTests(TestCase):
    def setUp(self):
        self.user_a = User.objects.create_user(username="simusera", email="s1@example.com", password="password123")
        self.user_b = User.objects.create_user(username="simuserb", email="s2@example.com", password="password123")

        self.upload_a = BillingUpload.objects.create(
            uploaded_by=self.user_a,
            upload_type="Billing Report",
            original_filename="user_a_billing.csv"
        )
        self.upload_b = BillingUpload.objects.create(
            uploaded_by=self.user_b,
            upload_type="Billing Report",
            original_filename="user_b_billing.csv"
        )

        # Baseline: USD Compute 100.00, EUR Database 50.00
        # Let's use middle of last month to make sure it's completed
        from django.utils import timezone
        today = timezone.localdate()
        first_of_this_month = today.replace(day=1)
        self.last_month_date = first_of_this_month - datetime.timedelta(days=15)

        self.r_usd = BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_id="comp-usd",
            cost=Decimal("100.00"),
            currency="USD",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )
        self.r_eur = BillingRecord.objects.create(
            upload=self.upload_a,
            service="Database",
            resource_id="db-eur",
            cost=Decimal("50.00"),
            currency="EUR",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )

        # Recommendation for User A:
        from ai_engine.models import Recommendation
        self.rec_a = Recommendation.objects.create(
            user=self.user_a,
            recommendation_type="RIGHTSIZE_REVIEW",
            service_name="Compute",
            estimated_monthly_savings=Decimal("15.00"),
            currency="USD",
            fingerprint="fingerprint_rec_a"
        )

        # Recommendation for User B:
        self.rec_b = Recommendation.objects.create(
            user=self.user_b,
            recommendation_type="RIGHTSIZE_REVIEW",
            service_name="Compute",
            estimated_monthly_savings=Decimal("20.00"),
            currency="USD",
            fingerprint="fingerprint_rec_b"
        )

    def test_validation_percentage_limits(self):
        from analytics.services.cost_simulator import run_cost_simulation

        # Percentage below 0
        actions_neg = [{"action_type": "PERCENT_DECREASE", "service": "Compute", "value": "-5"}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_neg)

        # Percentage above 1000
        actions_high = [{"action_type": "PERCENT_INCREASE", "service": "Compute", "value": "1001"}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_high)

    def test_validation_fixed_amount_limits(self):
        from analytics.services.cost_simulator import run_cost_simulation

        # Fixed amount below 0
        actions_neg = [{"action_type": "FIXED_DECREASE", "service": "Compute", "currency": "USD", "value": "-10"}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_neg)

        # Fixed amount above 10000000
        actions_high = [{"action_type": "FIXED_INCREASE", "service": "Compute", "currency": "USD", "value": "10000001"}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_high)

    def test_invalid_action_type(self):
        from analytics.services.cost_simulator import run_cost_simulation
        actions = [{"action_type": "INVALID_TYPE", "service": "Compute", "value": "10"}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions)

    def test_malformed_custom_dates(self):
        from analytics.services.cost_simulator import run_cost_simulation
        actions = [{"action_type": "PERCENT_DECREASE", "service": "Compute", "value": "20"}]

        # Malformed custom start date
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "CUSTOM", start_date_str="2026/06/01", end_date_str="2026-06-30", actions=actions)

        # Malformed custom end date
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "CUSTOM", start_date_str="2026-06-01", end_date_str="30-06-2026", actions=actions)

        # Missing custom dates
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "CUSTOM", actions=actions)

        # start_date > end_date
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "CUSTOM", start_date_str="2026-07-01", end_date_str="2026-06-30", actions=actions)

    def test_empty_scenario_and_missing_billing_records(self):
        from analytics.services.cost_simulator import run_cost_simulation
        
        # Empty scenario
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=[])

        # Selected period has no billing records
        # Let's search far in the future
        actions = [{"action_type": "PERCENT_DECREASE", "service": "Compute", "value": "20"}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "CUSTOM", start_date_str="2035-01-01", end_date_str="2035-01-31", actions=actions)

    def test_service_and_currency_validation(self):
        from analytics.services.cost_simulator import run_cost_simulation

        # Unavailable/foreign service
        actions_svc = [{"action_type": "PERCENT_DECREASE", "service": "ForeignService", "value": "20"}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_svc)

        # Invalid currency for targeted service
        # Compute exists in USD, but not in EUR for this user in LAST_MONTH
        actions_curr = [{"action_type": "FIXED_DECREASE", "service": "Compute", "currency": "EUR", "value": "10"}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_curr)

    def test_blank_metadata_normalization(self):
        # Create a record with blank service and currency
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="",
            resource_id="blank-resource",
            cost=Decimal("30.00"),
            currency="",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )
        from analytics.services.cost_simulator import run_cost_simulation
        
        # Blank service/currency should match target "Unknown Service" / "UNKNOWN"
        actions = [{"action_type": "PERCENT_DECREASE", "service": "Unknown Service", "currency": "UNKNOWN", "value": "50"}]
        res = run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions)
        
        self.assertIn("UNKNOWN", res["currency_results"])
        unk_res = res["currency_results"]["UNKNOWN"]
        # Baseline = 30.00, Decreased by 50% = 15.00
        self.assertEqual(unk_res["baseline_cost"], Decimal("30.00"))
        self.assertEqual(unk_res["simulated_cost"], Decimal("15.00"))
        self.assertEqual(unk_res["absolute_change"], Decimal("-15.00"))

    def test_zero_baseline_percentage_handling(self):
        # We need a user with baseline 0.00 but with billing records in that period
        # Create a record with cost 0.00 in USD
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="ZeroSvc",
            resource_id="zero-res",
            cost=Decimal("0.00"),
            currency="USD",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )
        from analytics.services.cost_simulator import run_cost_simulation
        
        # Now run action on ZeroSvc which has 0.00 baseline
        actions = [{"action_type": "PERCENT_DECREASE", "service": "ZeroSvc", "currency": "USD", "value": "20"}]
        
        # Delete non-zero records to force USD baseline to 0.00
        BillingRecord.objects.filter(cost__gt=0).delete()
        
        res = run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions)
        usd_res = res["currency_results"]["USD"]
        self.assertEqual(usd_res["baseline_cost"], Decimal("0.00"))
        self.assertIsNone(usd_res["percentage_change"])
        self.assertEqual(usd_res["percentage_change_reason"], "ZERO_BASELINE")

    def test_explicit_conflict_checks(self):
        from analytics.services.cost_simulator import run_cost_simulation

        # 1. Duplicate manual actions targeting same (service, currency)
        actions_dup_manual = [
            {"action_type": "PERCENT_DECREASE", "service": "Compute", "currency": "USD", "value": "10"},
            {"action_type": "PERCENT_INCREASE", "service": "Compute", "currency": "USD", "value": "20"}
        ]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_dup_manual)

        # 2. Duplicate recommendation IDs
        actions_dup_rec = [
            {"action_type": "RECOMMENDATION_SAVINGS", "recommendation_id": self.rec_a.id},
            {"action_type": "RECOMMENDATION_SAVINGS", "recommendation_id": self.rec_a.id}
        ]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_dup_rec)

        # 3. Manual action and recommendation action targeting same (service, currency)
        actions_conflict = [
            {"action_type": "RECOMMENDATION_SAVINGS", "recommendation_id": self.rec_a.id},
            {"action_type": "PERCENT_DECREASE", "service": "Compute", "currency": "USD", "value": "10"}
        ]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_conflict)

    def test_recommendation_edge_cases(self):
        from analytics.services.cost_simulator import run_cost_simulation
        from ai_engine.models import Recommendation

        # A. Recommendation with NULL savings
        rec_null = Recommendation.objects.create(
            user=self.user_a,
            recommendation_type="STORAGE_OPTIMIZATION",
            service_name="Storage",
            estimated_monthly_savings=None,
            currency="USD",
            fingerprint="fingerprint_null"
        )
        actions_null = [{"action_type": "RECOMMENDATION_SAVINGS", "recommendation_id": rec_null.id}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_null)

        # B. Recommendation without valid service target
        # Targets "NonexistentService" in USD
        rec_no_svc = Recommendation.objects.create(
            user=self.user_a,
            recommendation_type="STORAGE_OPTIMIZATION",
            service_name="NonexistentService",
            estimated_monthly_savings=Decimal("10.00"),
            currency="USD",
            fingerprint="fingerprint_no_svc"
        )
        actions_no_svc = [{"action_type": "RECOMMENDATION_SAVINGS", "recommendation_id": rec_no_svc.id}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_no_svc)

        # C. Foreign recommendation (User A trying to access User B's recommendation)
        actions_foreign = [{"action_type": "RECOMMENDATION_SAVINGS", "recommendation_id": self.rec_b.id}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_foreign)

        # D. Recommendation currency mismatch
        # Create a record for Service Storage in EUR
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Storage",
            resource_id="storage-eur",
            cost=Decimal("40.00"),
            currency="EUR",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )
        # Recommendation targets Storage in USD, but Storage only exists in EUR in the period
        rec_mismatch = Recommendation.objects.create(
            user=self.user_a,
            recommendation_type="STORAGE_OPTIMIZATION",
            service_name="Storage",
            estimated_monthly_savings=Decimal("10.00"),
            currency="USD",
            fingerprint="fingerprint_mismatch"
        )
        actions_mismatch = [{"action_type": "RECOMMENDATION_SAVINGS", "recommendation_id": rec_mismatch.id}]
        with self.assertRaises(ValueError):
            run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_mismatch)

    def test_mathematical_correctness(self):
        from analytics.services.cost_simulator import run_cost_simulation

        # 1. Multi-currency percentage behavior
        # Compute exists in USD (100.00). Let's create Compute in EUR (20.00) too.
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_id="comp-eur",
            cost=Decimal("20.00"),
            currency="EUR",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )
        # Apply manual percentage decrease: 20% on Compute for ALL currencies
        actions = [{"action_type": "PERCENT_DECREASE", "service": "Compute", "value": "20"}]
        res = run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions)
        
        # USD: Compute baseline 100.00 -> simulated 80.00. Total simulated = 80.00
        self.assertEqual(res["currency_results"]["USD"]["baseline_cost"], Decimal("100.00"))
        self.assertEqual(res["currency_results"]["USD"]["simulated_cost"], Decimal("80.00"))
        self.assertEqual(res["currency_results"]["USD"]["absolute_change"], Decimal("-20.00"))
        self.assertEqual(res["currency_results"]["USD"]["percentage_change"], Decimal("-20.00"))
        
        # EUR: Compute baseline 20.00, Database baseline 50.00.
        # Compute simulated = 16.00, Database simulated = 50.00. Total simulated = 66.00.
        # Baseline total = 70.00. Absolute change = -4.00. Percentage change = -4/70 * 100 = -5.71%
        self.assertEqual(res["currency_results"]["EUR"]["baseline_cost"], Decimal("70.00"))
        self.assertEqual(res["currency_results"]["EUR"]["simulated_cost"], Decimal("66.00"))
        self.assertEqual(res["currency_results"]["EUR"]["absolute_change"], Decimal("-4.00"))
        self.assertEqual(res["currency_results"]["EUR"]["percentage_change"], Decimal("-5.71"))

        # 2. Fixed action affects only selected currency
        # database + 10 EUR
        actions_fixed = [{"action_type": "FIXED_INCREASE", "service": "Database", "currency": "EUR", "value": "10"}]
        res_fixed = run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_fixed)
        self.assertEqual(res_fixed["currency_results"]["USD"]["simulated_cost"], Decimal("100.00")) # unchanged
        self.assertEqual(res_fixed["currency_results"]["EUR"]["simulated_cost"], Decimal("80.00")) # 70.00 + 10.00

        # 3. Negative simulated cost flooring
        # decrease Compute (100.00) by 120 USD (fixed)
        actions_floor = [{"action_type": "FIXED_DECREASE", "service": "Compute", "currency": "USD", "value": "120"}]
        res_floor = run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_floor)
        self.assertEqual(res_floor["currency_results"]["USD"]["simulated_cost"], Decimal("0.00"))

        # 4. Exact Decimal rounding (e.g. baseline 99.99 with 10% increase)
        # Delete old records first
        BillingRecord.objects.all().delete()
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_id="comp-round",
            cost=Decimal("99.99"),
            currency="USD",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )
        actions_round = [{"action_type": "PERCENT_INCREASE", "service": "Compute", "currency": "USD", "value": "10"}]
        res_round = run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions_round)
        # 99.99 * 1.10 = 109.989 -> rounds to 109.99
        self.assertEqual(res_round["currency_results"]["USD"]["simulated_cost"], Decimal("109.99"))

    def test_database_immutability(self):
        from analytics.services.cost_simulator import run_cost_simulation
        from ai_engine.models import Recommendation
        actions = [{"action_type": "PERCENT_DECREASE", "service": "Compute", "value": "10"}]
        
        # Check counts before simulation
        record_count = BillingRecord.objects.count()
        rec_count = Recommendation.objects.count()
        
        run_cost_simulation(self.user_a, "LAST_MONTH", actions=actions)
        
        # Check counts after simulation
        self.assertEqual(BillingRecord.objects.count(), record_count)
        self.assertEqual(Recommendation.objects.count(), rec_count)
        
        # Check actual values are untouched
        self.assertEqual(BillingRecord.objects.get(pk=self.r_usd.pk).cost, Decimal("100.00"))
        self.assertEqual(Recommendation.objects.get(pk=self.rec_a.pk).estimated_monthly_savings, Decimal("15.00"))

    def test_view_auth_and_user_isolation(self):
        url = reverse("cost-simulator")
        
        # Unauthenticated request redirects
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        
        # Authenticated access
        self.client.login(username="simusera", password="password123")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("open_recommendations", response.context)
        
        # Test User B isolation in POST
        self.client.logout()
        self.client.login(username="simuserb", password="password123")
        # User B has no records for last month, running simulation fails
        post_data = {
            "period": "LAST_MONTH",
            "action_type[]": ["PERCENT_DECREASE"],
            "service[]": ["Compute"],
            "currency[]": ["USD"],
            "value[]": ["20"],
            "recommendation_id[]": [""]
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 200)
        # Check for message error "No billing records found"
        self.assertContains(response, "No billing records found for the selected time period.")

    def test_dashboard_card_links_to_simulator(self):
        self.client.login(username="simusera", password="password123")
        response = self.client.get(reverse("dashboard-home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("cost-simulator"))


class ExecutiveReportTests(TestCase):
    def setUp(self):
        from ai_engine.models import Recommendation
        self.user_a = User.objects.create_user(username="repuser_a", email="ra@example.com", password="password123")
        self.user_b = User.objects.create_user(username="repuser_b", email="rb@example.com", password="password123")

        self.upload_a = BillingUpload.objects.create(
            uploaded_by=self.user_a,
            upload_type="Billing Report",
            original_filename="user_a.csv"
        )
        self.upload_b = BillingUpload.objects.create(
            uploaded_by=self.user_b,
            upload_type="Billing Report",
            original_filename="user_b.csv"
        )

        from django.utils import timezone
        today = timezone.localdate()
        first_of_this_month = today.replace(day=1)
        self.last_month_date = first_of_this_month - datetime.timedelta(days=2)
        
        # User A billing records
        self.r1 = BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_id="comp-1",
            cost=Decimal("100.00"),
            currency="USD",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )
        self.r2 = BillingRecord.objects.create(
            upload=self.upload_a,
            service="Database",
            resource_id="db-1",
            cost=Decimal("200.00"),
            currency="USD",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )

        # User B billing records
        self.r_b = BillingRecord.objects.create(
            upload=self.upload_b,
            service="Compute",
            resource_id="comp-b",
            cost=Decimal("500.00"),
            currency="USD",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )

        # User A Anomalies
        self.anom_crit = CostAnomaly.objects.create(
            user=self.user_a,
            billing_upload=self.upload_a,
            anomaly_type="RESOURCE_SPIKE",
            detected_date=self.last_month_date,
            service_name="Compute",
            actual_cost=Decimal("120.00"),
            expected_cost=Decimal("20.00"),
            deviation_percentage=Decimal("500.00"),
            severity="CRITICAL",
            status="OPEN"
        )
        self.anom_low = CostAnomaly.objects.create(
            user=self.user_a,
            billing_upload=self.upload_a,
            anomaly_type="RESOURCE_SPIKE",
            detected_date=self.last_month_date,
            service_name="Compute",
            resource_id="comp-low",
            actual_cost=Decimal("30.00"),
            expected_cost=Decimal("20.00"),
            deviation_percentage=Decimal("50.00"),
            severity="LOW",
            status="OPEN"
        )

        # User B Anomalies
        self.anom_b = CostAnomaly.objects.create(
            user=self.user_b,
            billing_upload=self.upload_b,
            anomaly_type="DAILY_SPIKE",
            detected_date=self.last_month_date,
            actual_cost=Decimal("200.00"),
            expected_cost=Decimal("100.00"),
            deviation_percentage=Decimal("100.00"),
            severity="CRITICAL",
            status="OPEN"
        )

        # User A Waste Findings
        self.w1 = WasteFinding.objects.create(
            user=self.user_a,
            waste_type="POSSIBLE_UNUSED_STORAGE",
            resource_id="stale-vol",
            service_name="Storage",
            currency="USD",
            first_seen=self.last_month_date - datetime.timedelta(days=5),
            last_seen=self.last_month_date + datetime.timedelta(days=5),
            observation_days=10,
            calendar_span_days=10,
            coverage_ratio=Decimal("1.0"),
            total_cost=Decimal("40.00"),
            average_daily_cost=Decimal("4.00"),
            estimated_monthly_cost=Decimal("120.00"),
            estimated_monthly_savings=Decimal("120.00"),
            confidence="HIGH",
            status="OPEN"
        )

        # User B Waste Findings
        self.w_b = WasteFinding.objects.create(
            user=self.user_b,
            waste_type="PERSISTENT_LOW_COST_RESOURCE",
            service_name="Compute",
            currency="USD",
            first_seen=self.last_month_date,
            last_seen=self.last_month_date,
            observation_days=1,
            calendar_span_days=1,
            coverage_ratio=Decimal("1.0"),
            total_cost=Decimal("10.00"),
            average_daily_cost=Decimal("10.00"),
            estimated_monthly_cost=Decimal("300.00"),
            estimated_monthly_savings=Decimal("300.00"),
            confidence="HIGH",
            status="OPEN"
        )

        # User A Recommendations
        self.rec1 = Recommendation.objects.create(
            user=self.user_a,
            recommendation_type="STORAGE_OPTIMIZATION",
            service_name="Storage",
            source_type="WASTE_FINDING",
            source_id=self.w1.id,
            estimated_monthly_savings=Decimal("120.00"),
            currency="USD",
            savings_source="WASTE_FINDING",
            priority="HIGH",
            confidence="HIGH",
            status="OPEN",
            fingerprint="user_a_rec_1"
        )
        self.rec2 = Recommendation.objects.create(
            user=self.user_a,
            recommendation_type="RIGHTSIZE_REVIEW",
            service_name="Compute",
            source_type="WASTE_FINDING",
            source_id=self.w1.id,
            estimated_monthly_savings=Decimal("120.00"),
            currency="USD",
            savings_source="WASTE_FINDING",
            priority="LOW",
            confidence="MEDIUM",
            status="OPEN",
            fingerprint="user_a_rec_2"
        )
        
        # Override auto_now_add detected_at dates for period query isolation
        dt = datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        Recommendation.objects.filter(pk__in=[self.rec1.pk, self.rec2.pk]).update(detected_at=dt)

    def test_access_requires_login(self):
        url = reverse("cost-report")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

        response = self.client.post(url, {"period": "LAST_MONTH"})
        self.assertEqual(response.status_code, 302)

    def test_user_isolation(self):
        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        
        # User A's records count should be 2 (self.r1 and self.r2)
        self.assertEqual(data["total_records_count"], 2)
        
        # Excludes User B anomaly
        self.assertEqual(len(data["anomalies"]), 2)
        self.assertNotIn(self.anom_b, data["anomalies"])

        # Excludes User B waste finding
        self.assertEqual(len(data["waste_findings"]), 1)
        self.assertNotIn(self.w_b, data["waste_findings"])

        # Excludes User B recommendation
        self.assertEqual(len(data["recommendations"]), 2)

    def test_period_selections(self):
        from analytics.services.report_service import collect_report_data
        
        # Last Month
        data_last = collect_report_data(self.user_a, "LAST_MONTH")
        self.assertEqual(data_last["total_records_count"], 2)

        # Current Month (no records exist, should show 0)
        data_curr = collect_report_data(self.user_a, "CURRENT_MONTH")
        self.assertEqual(data_curr["total_records_count"], 0)

        # Last 30 Days
        data_30 = collect_report_data(self.user_a, "LAST_30_DAYS")
        # should cover last month date
        self.assertEqual(data_30["total_records_count"], 2)

        # Custom date validation
        # valid custom
        sd = self.last_month_date.strftime("%Y-%m-%d")
        ed = self.last_month_date.strftime("%Y-%m-%d")
        data_cust = collect_report_data(self.user_a, "CUSTOM", start_date_str=sd, end_date_str=ed)
        self.assertEqual(data_cust["total_records_count"], 2)

        # malformed dates or start_date > end_date
        with self.assertRaises(ValueError):
            collect_report_data(self.user_a, "CUSTOM", start_date_str="2026/01/01", end_date_str="2026-01-31")
        with self.assertRaises(ValueError):
            collect_report_data(self.user_a, "CUSTOM", start_date_str="2026-02-01", end_date_str="2026-01-31")
        with self.assertRaises(ValueError):
            collect_report_data(self.user_a, "CUSTOM")

    def test_financial_aggregations_and_normalization(self):
        # Create record with empty service, currency, region
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="",
            resource_id="blank-res",
            cost=Decimal("50.00"),
            currency="",
            region="",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )
        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        
        # normalizes null currency to UNKNOWN
        self.assertIn("UNKNOWN", data["total_costs"])
        self.assertEqual(data["total_costs"]["UNKNOWN"], Decimal("50.00"))

        # normalizes null service to Unknown Service
        services = [s["service"] for s in data["services_breakdown"]["UNKNOWN"]]
        self.assertIn("Unknown Service", services)

        # normalizes null region to Unknown Region
        regions = [r["region"] for r in data["regions_breakdown"]["UNKNOWN"]]
        self.assertIn("Unknown Region", regions)

        # unique resources identity priority test
        # self.r1 has resource_id="comp-1".
        # Create a record with blank resource_id but with resource_name="my-res"
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute",
            resource_name="my-res",
            cost=Decimal("10.00"),
            currency="USD",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )
        data = collect_report_data(self.user_a, "LAST_MONTH")
        res_names = [r["resource"] for r in data["top_resources"]["USD"]]
        self.assertIn("my-res", res_names)

    def test_deduplication(self):
        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        
        # rec1 and rec2 both point to self.w1.id (WasteFinding).
        # Savings total should count self.w1 only once: 120.00 USD
        self.assertEqual(data["potential_savings"]["USD"], Decimal("120.00"))

    def test_forecast_integration(self):
        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        
        # forecast is enabled by default
        self.assertIn("forecast_results", data)
        # USD has insufficient history (needs 3 completed months)
        self.assertIn("USD", data["forecast_results"])
        self.assertFalse(data["forecast_results"]["USD"]["forecast_available"])

    def test_optional_section_validation(self):
        self.client.login(username="repuser_a", password="password123")
        url = reverse("cost-report")
        
        # POST request disabling cost_breakdown and forecast
        post_data = {
            "period": "LAST_MONTH",
            "sections[]": ["anomalies", "waste", "recommendations"]
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        
        # check PDF signature
        pdf_bytes = b"".join(response.streaming_content) if hasattr(response, "streaming_content") else response.content
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_empty_dataset_pdf_generation(self):
        # User A has no records for the current month
        self.client.login(username="repuser_a", password="password123")
        url = reverse("cost-report")
        post_data = {
            "period": "CURRENT_MONTH",
            "sections[]": ["cost_breakdown", "anomalies", "waste", "recommendations", "forecast"]
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 200)
        
        pdf_bytes = b"".join(response.streaming_content) if hasattr(response, "streaming_content") else response.content
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_xml_characters_escaping(self):
        # Create billing record and anomaly with XML special characters
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute & Storage <Test>",
            resource_id="r&d-node",
            cost=Decimal("15.00"),
            currency="USD",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )
        
        self.client.login(username="repuser_a", password="password123")
        url = reverse("cost-report")
        response = self.client.post(url, {"period": "LAST_MONTH"})
        self.assertEqual(response.status_code, 200)
        pdf_bytes = b"".join(response.streaming_content) if hasattr(response, "streaming_content") else response.content
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_database_immutability(self):
        from analytics.services.report_service import collect_report_data
        from analytics.services.pdf_report_generator import generate_pdf_report
        from ai_engine.models import Recommendation
        
        # snapshot counts
        upload_count = BillingUpload.objects.count()
        record_count = BillingRecord.objects.count()
        anomaly_count = CostAnomaly.objects.count()
        waste_count = WasteFinding.objects.count()
        rec_count = Recommendation.objects.count()

        data = collect_report_data(self.user_a, "LAST_MONTH")
        generate_pdf_report(data)

        # counts should remain unchanged
        self.assertEqual(BillingUpload.objects.count(), upload_count)
        self.assertEqual(BillingRecord.objects.count(), record_count)
        self.assertEqual(CostAnomaly.objects.count(), anomaly_count)
        self.assertEqual(WasteFinding.objects.count(), waste_count)
        self.assertEqual(Recommendation.objects.count(), rec_count)

    def test_multi_page_pdf_generation(self):
        # Clear NumberedCanvas instances
        from analytics.services.pdf_report_generator import NumberedCanvas
        NumberedCanvas.drawn_instances = []

        # Request report for User A for LAST_MONTH
        # Create a large dataset to force multiple pages
        for i in range(35):
            BillingRecord.objects.create(
                upload=self.upload_a,
                service=f"ComputeService{i}",
                resource_id=f"res-{i}",
                cost=Decimal("10.00"),
                currency="USD",
                usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
            )

        self.client.login(username="repuser_a", password="password123")
        url = reverse("cost-report")
        response = self.client.post(url, {"period": "LAST_MONTH"})
        self.assertEqual(response.status_code, 200)
        pdf_bytes = b"".join(response.streaming_content) if hasattr(response, "streaming_content") else response.content
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

        self.assertTrue(len(NumberedCanvas.drawn_instances) > 0)
        canvas_inst = NumberedCanvas.drawn_instances[-1]
        self.assertTrue(len(canvas_inst._saved_page_states) > 1) # Must span multiple pages
        
        self.assertTrue(len(canvas_inst.pages_decorated) > 0)
        first_decor = canvas_inst.pages_decorated[0]
        self.assertEqual(first_decor["page_num"], 2)
        self.assertEqual(first_decor["page_count"], len(canvas_inst._saved_page_states))
        self.assertIn(f"Page 2 of {len(canvas_inst._saved_page_states)}", first_decor["page_text"])

    def test_last_30_days_exact_boundary(self):
        from analytics.services.report_service import resolve_report_period
        start_date, end_date = resolve_report_period("LAST_30_DAYS")
        # should contain exactly 30 inclusive calendar dates
        num_days = (end_date - start_date).days + 1
        self.assertEqual(num_days, 30)

    def test_year_boundary(self):
        from unittest.mock import patch
        from analytics.services.report_service import resolve_report_period
        # Mock timezone.localdate to return Jan 1st of a new year
        with patch("django.utils.timezone.localdate", return_value=datetime.date(2027, 1, 1)):
            # Last Month of previous year (December 2026)
            lm_start, lm_end = resolve_report_period("LAST_MONTH")
            self.assertEqual(lm_start, datetime.date(2026, 12, 1))
            self.assertEqual(lm_end, datetime.date(2026, 12, 31))

            # Current Month
            cm_start, cm_end = resolve_report_period("CURRENT_MONTH")
            self.assertEqual(cm_start, datetime.date(2027, 1, 1))
            self.assertEqual(cm_end, datetime.date(2027, 1, 31))

    def test_anomaly_date_and_severity_ordering_and_ties(self):
        # Clear existing anomalies
        CostAnomaly.objects.filter(user=self.user_a).delete()

        # Create anomalies with different dates, severities, pks
        # CRITICAL, HIGH, MEDIUM, LOW
        # For ties, detected_date desc, then pk asc
        a1 = CostAnomaly.objects.create(
            user=self.user_a,
            billing_upload=self.upload_a,
            anomaly_type="RESOURCE_SPIKE",
            detected_date=self.last_month_date,
            severity="LOW",
            actual_cost=Decimal("1.00"), expected_cost=Decimal("0.00"), deviation_percentage=Decimal("0.00"),
            status="OPEN",
            resource_id="res-1"
        )
        a2 = CostAnomaly.objects.create(
            user=self.user_a,
            billing_upload=self.upload_a,
            anomaly_type="RESOURCE_SPIKE",
            detected_date=self.last_month_date + datetime.timedelta(days=1), # newer date -> should come first in tie
            severity="HIGH",
            actual_cost=Decimal("1.00"), expected_cost=Decimal("0.00"), deviation_percentage=Decimal("0.00"),
            status="OPEN",
            resource_id="res-2"
        )
        a3 = CostAnomaly.objects.create(
            user=self.user_a,
            billing_upload=self.upload_a,
            anomaly_type="RESOURCE_SPIKE",
            detected_date=self.last_month_date, # older date -> should come second
            severity="HIGH",
            actual_cost=Decimal("1.00"), expected_cost=Decimal("0.00"), deviation_percentage=Decimal("0.00"),
            status="OPEN",
            resource_id="res-3"
        )
        a4 = CostAnomaly.objects.create(
            user=self.user_a,
            billing_upload=self.upload_a,
            anomaly_type="RESOURCE_SPIKE",
            detected_date=self.last_month_date, # same severity, same date -> pk tie breaker (a4 has larger pk, so after a3)
            severity="HIGH",
            actual_cost=Decimal("1.00"), expected_cost=Decimal("0.00"), deviation_percentage=Decimal("0.00"),
            status="OPEN",
            resource_id="res-4"
        )
        a5 = CostAnomaly.objects.create(
            user=self.user_a,
            billing_upload=self.upload_a,
            anomaly_type="RESOURCE_SPIKE",
            detected_date=self.last_month_date,
            severity="CRITICAL",
            actual_cost=Decimal("1.00"), expected_cost=Decimal("0.00"), deviation_percentage=Decimal("0.00"),
            status="OPEN",
            resource_id="res-5"
        )

        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        anomalies_list = data["anomalies"]
        
        # Expected order:
        # 1. a5 (CRITICAL)
        # 2. a2 (HIGH, newer date: last_month_date + 1)
        # 3. a3 (HIGH, older date, lower pk)
        # 4. a4 (HIGH, older date, higher pk)
        # 5. a1 (LOW)
        self.assertEqual([a.pk for a in anomalies_list], [a5.pk, a2.pk, a3.pk, a4.pk, a1.pk])

    def test_waste_observation_overlap_boundaries(self):
        # Clear existing waste findings
        WasteFinding.objects.filter(user=self.user_a).delete()

        # Report period is LAST_MONTH. Let's find dates
        from analytics.services.report_service import resolve_report_period
        start_date, end_date = resolve_report_period("LAST_MONTH")

        # 1. strictly before: last_seen < start_date -> excluded
        w_before = WasteFinding.objects.create(
            user=self.user_a, waste_type="POSSIBLE_UNUSED_STORAGE", confidence="HIGH",
            first_seen=start_date - datetime.timedelta(days=10),
            last_seen=start_date - datetime.timedelta(days=1),
            estimated_monthly_savings=Decimal("10"), currency="USD", total_cost=Decimal("0.00"),
            average_daily_cost=Decimal("0.00"), estimated_monthly_cost=Decimal("0.00"), status="OPEN",
            observation_days=10, calendar_span_days=10, coverage_ratio=Decimal("1.0"),
            resource_key="stale-vol-before"
        )

        # 2. strictly after: first_seen > end_date -> excluded
        w_after = WasteFinding.objects.create(
            user=self.user_a, waste_type="POSSIBLE_UNUSED_STORAGE", confidence="HIGH",
            first_seen=end_date + datetime.timedelta(days=1),
            last_seen=end_date + datetime.timedelta(days=10),
            estimated_monthly_savings=Decimal("10"), currency="USD", total_cost=Decimal("0.00"),
            average_daily_cost=Decimal("0.00"), estimated_monthly_cost=Decimal("0.00"), status="OPEN",
            observation_days=10, calendar_span_days=10, coverage_ratio=Decimal("1.0"),
            resource_key="stale-vol-after"
        )

        # 3. left boundary overlap: last_seen = start_date -> included
        w_left = WasteFinding.objects.create(
            user=self.user_a, waste_type="POSSIBLE_UNUSED_STORAGE", confidence="HIGH",
            first_seen=start_date - datetime.timedelta(days=5),
            last_seen=start_date,
            estimated_monthly_savings=Decimal("10"), currency="USD", total_cost=Decimal("0.00"),
            average_daily_cost=Decimal("0.00"), estimated_monthly_cost=Decimal("0.00"), status="OPEN",
            observation_days=10, calendar_span_days=10, coverage_ratio=Decimal("1.0"),
            resource_key="stale-vol-left"
        )

        # 4. right boundary overlap: first_seen = end_date -> included
        w_right = WasteFinding.objects.create(
            user=self.user_a, waste_type="POSSIBLE_UNUSED_STORAGE", confidence="HIGH",
            first_seen=end_date,
            last_seen=end_date + datetime.timedelta(days=5),
            estimated_monthly_savings=Decimal("10"), currency="USD", total_cost=Decimal("0.00"),
            average_daily_cost=Decimal("0.00"), estimated_monthly_cost=Decimal("0.00"), status="OPEN",
            observation_days=10, calendar_span_days=10, coverage_ratio=Decimal("1.0"),
            resource_key="stale-vol-right"
        )

        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        w_pks = [w.pk for w in data["waste_findings"]]
        
        self.assertIn(w_left.pk, w_pks)
        self.assertIn(w_right.pk, w_pks)
        self.assertNotIn(w_before.pk, w_pks)
        self.assertNotIn(w_after.pk, w_pks)

    def test_recommendation_period_semantics(self):
        from ai_engine.models import Recommendation
        Recommendation.objects.filter(user=self.user_a).delete()

        from analytics.services.report_service import resolve_report_period
        start_date, end_date = resolve_report_period("LAST_MONTH")

        # Create recommendation inside period
        dt_in = datetime.datetime.combine(start_date + datetime.timedelta(days=5), datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        r_in = Recommendation.objects.create(
            user=self.user_a, recommendation_type="RIGHTSIZE_REVIEW", priority="HIGH",
            savings_source="WASTE_FINDING", status="OPEN", fingerprint="rec_in"
        )
        Recommendation.objects.filter(pk=r_in.pk).update(detected_at=dt_in)

        # Create recommendation outside period
        dt_out = datetime.datetime.combine(end_date + datetime.timedelta(days=5), datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        r_out = Recommendation.objects.create(
            user=self.user_a, recommendation_type="RIGHTSIZE_REVIEW", priority="HIGH",
            savings_source="WASTE_FINDING", status="OPEN", fingerprint="rec_out"
        )
        Recommendation.objects.filter(pk=r_out.pk).update(detected_at=dt_out)

        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        rec_pks = [r.pk for r in data["recommendations"]]
        self.assertIn(r_in.pk, rec_pks)
        self.assertNotIn(r_out.pk, rec_pks)

    def test_arbitrary_optional_section_names_ignored(self):
        self.client.login(username="repuser_a", password="password123")
        url = reverse("cost-report")
        post_data = {
            "period": "LAST_MONTH",
            "sections[]": ["anomalies", "waste", "some_random_section_name"]
        }
        response = self.client.post(url, post_data)
        # Should generate PDF successfully and completely ignore "some_random_section_name"
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_cost_table_10_row_limit(self):
        # Create 15 distinct service types under USD
        for i in range(15):
            BillingRecord.objects.create(
                upload=self.upload_a,
                service=f"Svc-{i}",
                region=f"Reg-{i}",
                resource_id=f"Res-{i}",
                cost=Decimal("10.00"),
                currency="USD",
                usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
            )
        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        
        self.assertEqual(len(data["services_breakdown"]["USD"]), 10)
        self.assertEqual(len(data["regions_breakdown"]["USD"]), 10)
        self.assertEqual(len(data["top_resources"]["USD"]), 10)

    def test_anomaly_waste_recommendation_20_row_limits(self):
        # Clear existing
        CostAnomaly.objects.filter(user=self.user_a).delete()
        WasteFinding.objects.filter(user=self.user_a).delete()
        from ai_engine.models import Recommendation
        Recommendation.objects.filter(user=self.user_a).delete()

        # Create 25 anomalies, waste findings, recommendations
        for i in range(25):
            a = CostAnomaly.objects.create(
                user=self.user_a, billing_upload=self.upload_a, anomaly_type="RESOURCE_SPIKE",
                detected_date=self.last_month_date, severity="LOW", actual_cost=Decimal("1.00"),
                expected_cost=Decimal("0.00"), deviation_percentage=Decimal("0.00"), status="OPEN",
                resource_id=f"anom-limit-{i}"
            )
            w = WasteFinding.objects.create(
                user=self.user_a, waste_type="POSSIBLE_UNUSED_STORAGE", confidence="HIGH",
                first_seen=self.last_month_date, last_seen=self.last_month_date,
                estimated_monthly_savings=Decimal("10"), currency="USD", total_cost=Decimal("0.00"),
                average_daily_cost=Decimal("0.00"), estimated_monthly_cost=Decimal("0.00"), status="OPEN",
                observation_days=10, calendar_span_days=10, coverage_ratio=Decimal("1.0"),
                resource_key=f"waste-limit-{i}"
            )
            r = Recommendation.objects.create(
                user=self.user_a, recommendation_type="RIGHTSIZE_REVIEW", priority="HIGH",
                savings_source="WASTE_FINDING", status="OPEN", fingerprint=f"rec_limit_{i}"
            )
            Recommendation.objects.filter(pk=r.pk).update(
                detected_at=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
            )

        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        self.assertEqual(len(data["anomalies"]), 20)
        self.assertEqual(len(data["waste_findings"]), 20)
        self.assertEqual(len(data["recommendations"]), 20)

    def test_null_recommendation_savings(self):
        # Test that null estimated_monthly_savings is handled safely in sorting and deduplication
        from ai_engine.models import Recommendation
        Recommendation.objects.filter(user=self.user_a).delete()

        # Recommendation with NULL savings
        r_null = Recommendation.objects.create(
            user=self.user_a, recommendation_type="RIGHTSIZE_REVIEW", priority="HIGH",
            savings_source="WASTE_FINDING", estimated_monthly_savings=None, currency="USD",
            status="OPEN", fingerprint="rec_null_savings"
        )
        Recommendation.objects.filter(pk=r_null.pk).update(
            detected_at=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )

        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        # Should be included in recommendation list
        self.assertEqual(len(data["recommendations"]), 1)
        self.assertEqual(data["recommendations"][0].pk, r_null.pk)
        
        # Savings KPI under USD should be empty or 0.00 (not containing the null savings recommendation)
        self.assertEqual(data["potential_savings"].get("USD", Decimal("0.00")), Decimal("0.00"))

    def test_warnings_for_missing_metadata(self):
        # Clear existing records
        BillingRecord.objects.filter(upload=self.upload_a).delete()

        # Create record with blank currency, service, region
        r = BillingRecord.objects.create(
            upload=self.upload_a,
            service="",
            region="",
            currency="",
            cost=Decimal("10.00"),
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )

        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        
        # UNKNOWN warnings should be in warnings list
        self.assertIn("Billing records with missing or unknown currencies were detected (categorized as UNKNOWN).", data["warnings"])
        self.assertIn("Billing records with missing service names were detected (categorized as Unknown Service).", data["warnings"])
        self.assertIn("Billing records with missing regions were detected (categorized as Unknown Region).", data["warnings"])

    def test_forecast_state_variations_and_currency_isolation(self):
        # By default, not enough data (needs 3 completed months)
        from analytics.services.report_service import collect_report_data
        data = collect_report_data(self.user_a, "LAST_MONTH")
        self.assertFalse(data["forecast_results"]["USD"]["forecast_available"])
        self.assertIn("At least 3 months of historical billing data", data["forecast_results"]["USD"]["reason"])

        # Create enough data across 4 months for USD and EUR
        # Let's create uploads and records for Jan, Feb, Mar, Apr
        from unittest.mock import patch
        # Clear all records first to prevent overlap issues
        BillingRecord.objects.filter(upload__uploaded_by=self.user_a).delete()

        months = [
            datetime.date(2026, 1, 15),
            datetime.date(2026, 2, 15),
            datetime.date(2026, 3, 15),
            datetime.date(2026, 4, 15)
        ]
        for m in months:
            # USD records
            BillingRecord.objects.create(
                upload=self.upload_a, service="Compute", cost=Decimal("100.00"), currency="USD",
                usage_start=datetime.datetime.combine(m, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
            )
            # EUR records
            BillingRecord.objects.create(
                upload=self.upload_a, service="Compute", cost=Decimal("50.00"), currency="EUR",
                usage_start=datetime.datetime.combine(m, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
            )

        with patch("django.utils.timezone.localdate", return_value=datetime.date(2026, 5, 10)):
            data_fc = collect_report_data(self.user_a, "LAST_MONTH")
            
            # Verify USD forecast is available and isolated from EUR
            self.assertTrue(data_fc["forecast_results"]["USD"]["forecast_available"])
            self.assertEqual(data_fc["forecast_results"]["USD"]["next_month_forecast"], Decimal("100.00"))
            
            self.assertTrue(data_fc["forecast_results"]["EUR"]["forecast_available"])
            self.assertEqual(data_fc["forecast_results"]["EUR"]["next_month_forecast"], Decimal("50.00"))

    def test_unicode_and_xml_escaping_in_pdf(self):
        # Create record with XML characters and some unicode characters
        BillingRecord.objects.create(
            upload=self.upload_a,
            service="Compute & Storage <Instance> \u2605", # star unicode symbol
            region="us-phoenix-1",
            cost=Decimal("15.00"),
            currency="USD",
            usage_start=datetime.datetime.combine(self.last_month_date, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
        )
        self.client.login(username="repuser_a", password="password123")
        url = reverse("cost-report")
        response = self.client.post(url, {"period": "LAST_MONTH"})
        self.assertEqual(response.status_code, 200)
        pdf_bytes = b"".join(response.streaming_content) if hasattr(response, "streaming_content") else response.content
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_safe_error_handling(self):
        # Force collect_report_data to raise an unexpected database exception or similar RuntimeException
        from unittest.mock import patch
        with patch("analytics.services.report_service.collect_report_data_for_project", side_effect=RuntimeError("Secret DB Connection string /path/to/db failed")):
            self.client.login(username="repuser_a", password="password123")
            url = reverse("cost-report")
            response = self.client.post(url, {"period": "LAST_MONTH"})
            
            # View renders standard error message
            self.assertEqual(response.status_code, 200)
            # The template handles messages
            html = response.content.decode("utf-8")
            self.assertIn("Unable to generate the report. Please verify the report settings and try again.", html)
            self.assertNotIn("Secret DB Connection string", html)
            self.assertNotIn("RuntimeError", html)

    def test_no_gemini_invocations(self):
        # Verify that generating report makes no calls to the AI Engine provider or Gemini client.
        # Patch the genai client/GeminiProvider to raise an exception if it's called
        from unittest.mock import patch
        with patch("ai_engine.services.provider.GeminiProvider.generate_explanation", side_effect=Exception("Should not call LLM")):
            self.client.login(username="repuser_a", password="password123")
            url = reverse("cost-report")
            response = self.client.post(url, {"period": "LAST_MONTH"})
            self.assertEqual(response.status_code, 200)
            
    def test_report_db_immutability(self):
        # Snapshot the db
        counts = {
            "upload": BillingUpload.objects.count(),
            "record": BillingRecord.objects.count(),
            "anomaly": CostAnomaly.objects.count(),
            "waste": WasteFinding.objects.count(),
        }
        
        # Keep exact field values in check for one of anomalies
        anomaly_val = CostAnomaly.objects.first()
        old_cost = anomaly_val.actual_cost
        
        self.client.login(username="repuser_a", password="password123")
        url = reverse("cost-report")
        response = self.client.post(url, {"period": "LAST_MONTH"})
        self.assertEqual(response.status_code, 200)

        # Assert no writes occurred
        self.assertEqual(BillingUpload.objects.count(), counts["upload"])
        self.assertEqual(BillingRecord.objects.count(), counts["record"])
        self.assertEqual(CostAnomaly.objects.count(), counts["anomaly"])
        self.assertEqual(WasteFinding.objects.count(), counts["waste"])
        
        # Field values are unchanged
        self.assertEqual(CostAnomaly.objects.first().actual_cost, old_cost)





