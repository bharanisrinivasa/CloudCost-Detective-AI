from decimal import Decimal
import datetime
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from billing.models import BillingUpload, BillingRecord
from analytics.models import CostAnomaly
from analytics.services.anomaly_detector import (
    run_anomaly_detection_for_user,
    classify_severity,
    calculate_stats
)

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
