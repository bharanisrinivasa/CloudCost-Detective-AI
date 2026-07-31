import os
import datetime
import oci
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.exceptions import ImproperlyConfigured, ValidationError, PermissionDenied
from django.contrib.auth import get_user_model
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, Project
from billing.models import BillingUpload, BillingRecord
from analytics.models import WasteFinding
from oci_connector.models import (
    OCIConnection,
    OCIComputeInstance,
    OCIVolume,
    OCIObjectStorageBucket,
    OCIPublicIp,
    OCILoadBalancer,
    OCIResourceMetricSummary,
    OCISyncLog,
)
from oci_connector.services.encryption import encrypt_private_key, decrypt_private_key, get_encryption_key
from oci_connector.services.oci_client import build_oci_config, test_oci_connection_stages
from oci_connector.services.sync_service import OCISyncService, generate_source_fingerprint
from analytics.services.waste_detector import run_waste_detection_for_project
from analytics.services.recommendation_engine import run_recommendation_engine_for_project
from ai_engine.services.explanation_service import get_anomaly_deterministic_data, get_waste_deterministic_data

User = get_user_model()

@override_settings(OCI_ENCRYPTION_KEY="MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=")
class OCIConnectorTests(TestCase):
    def setUp(self):
        # Patch OCI load_private_key to prevent parsing fake private key in tests
        self.signer_patcher = patch("oci.signer.load_private_key")
        self.mock_load_private_key = self.signer_patcher.start()
        
        # Setup environment variable
        os.environ["OCI_ENCRYPTION_KEY"] = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI="
        
        # Create users, orgs, projects
        self.org1 = Organization.objects.create(name="Org 1", slug="org-1")
        self.org2 = Organization.objects.create(name="Org 2", slug="org-2")
        
        self.project1 = Project.objects.create(organization=self.org1, name="Project 1", slug="project-1")
        self.project2 = Project.objects.create(organization=self.org2, name="Project 2", slug="project-2")
        
        # User roles mapping
        self.owner = User.objects.create_user(username="owner", email="owner@org1.com", password="pwd")
        self.admin = User.objects.create_user(username="admin", email="admin@org1.com", password="pwd")
        self.analyst = User.objects.create_user(username="analyst", email="analyst@org1.com", password="pwd")
        self.viewer = User.objects.create_user(username="viewer", email="viewer@org1.com", password="pwd")
        self.unrelated = User.objects.create_user(username="unrelated", email="unrelated@org2.com", password="pwd")
        
        OrganizationMembership.objects.create(user=self.owner, organization=self.org1, role="OWNER")
        OrganizationMembership.objects.create(user=self.admin, organization=self.org1, role="ADMIN")
        OrganizationMembership.objects.create(user=self.analyst, organization=self.org1, role="ANALYST")
        OrganizationMembership.objects.create(user=self.viewer, organization=self.org1, role="VIEWER")
        OrganizationMembership.objects.create(user=self.unrelated, organization=self.org2, role="OWNER")

        # Create connection
        self.connection = OCIConnection.objects.create(
            project=self.project1,
            name="Test Connection",
            tenancy_ocid="ocid1.tenancy.oc1..test",
            user_ocid="ocid1.user.oc1..test",
            fingerprint="20:3b:97:13:55:1c:5b:0d:d3:37:d8:50:4e:c5:3a:34",
            private_key_encrypted=encrypt_private_key("-----BEGIN RSA PRIVATE KEY-----\nMOCK\n-----END RSA PRIVATE KEY-----"),
            region="us-ashburn-1",
            compartment_ocid="ocid1.compartment.oc1..test",
        )

    def tearDown(self):
        self.signer_patcher.stop()

    # 1. Encryption and Fails Closed tests
    def test_encryption_decryption(self):
        plaintext = "this-is-a-secret-key"
        encrypted = encrypt_private_key(plaintext)
        decrypted = decrypt_private_key(encrypted)
        self.assertEqual(plaintext, decrypted)
        self.assertNotEqual(plaintext, encrypted)

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_encryption_key_fails_closed(self):
        if "OCI_ENCRYPTION_KEY" in os.environ:
            del os.environ["OCI_ENCRYPTION_KEY"]
        with self.assertRaises(ImproperlyConfigured):
            get_encryption_key()

    @patch.dict(os.environ, {"OCI_ENCRYPTION_KEY": "too-short"}, clear=True)
    def test_invalid_encryption_key_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured):
            get_encryption_key()

    # 2. Connection Diagnostics and Stages tests
    @patch("oci_connector.services.oci_client.OCIClientFactory")
    def test_connection_stages_success(self, MockFactory):
        mock_factory = MagicMock()
        MockFactory.return_value = mock_factory
        
        mock_id = mock_factory.get_identity_client.return_value
        mock_tenancy = MagicMock()
        mock_tenancy.name = "Tenancy"
        mock_id.get_tenancy.return_value = MagicMock(data=mock_tenancy)
        mock_id.list_compartments.return_value = MagicMock(data=[], has_next_page=False)

        stages = test_oci_connection_stages(self.connection)
        self.assertEqual(stages["Authentication"], "OK")
        self.assertEqual(stages["Compartment Access"], "OK")

    @patch("oci_connector.services.oci_client.OCIClientFactory")
    def test_connection_stages_authentication_failure(self, MockFactory):
        mock_factory = MagicMock()
        MockFactory.return_value = mock_factory
        
        mock_id = mock_factory.get_identity_client.return_value
        mock_id.get_tenancy.side_effect = oci.exceptions.ServiceError(
            status=401, code="NotAuthorizedOrNotFound", message="Auth failure", headers={}
        )

        stages = test_oci_connection_stages(self.connection)
        self.assertTrue(stages["Authentication"].startswith("FAILED"))
        self.assertEqual(stages["Compartment Access"], "FAILED")

    # 3. RBAC Capability & Project Boundary (IDOR) tests
    def test_rbac_view_connection_detail(self):
        url = reverse("oci_connector:connection-detail")
        for u in [self.owner, self.admin, self.analyst, self.viewer]:
            self.client.force_login(u)
            session = self.client.session
            session["active_project_id"] = self.project1.id
            session.save()
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)

        self.client.force_login(self.unrelated)
        session = self.client.session
        session["active_project_id"] = self.project2.id
        session.save()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No OCI Connection configured")

    def test_rbac_manage_connection_detail(self):
        url = reverse("oci_connector:connection-detail")
        post_data = {
            "name": "Updated Name",
            "tenancy_ocid": "ocid1.tenancy.oc1..test",
            "user_ocid": "ocid1.user.oc1..test",
            "fingerprint": "20:3b:97:13:55:1c:5b:0d:d3:37:d8:50:4e:c5:3a:34",
            "region": "us-ashburn-1",
            "compartment_ocid": "ocid1.compartment.oc1..test",
            "private_key": "********"
        }

        self.client.force_login(self.admin)
        session = self.client.session
        session["active_project_id"] = self.project1.id
        session.save()
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.name, "Updated Name")

        self.client.force_login(self.viewer)
        session = self.client.session
        session["active_project_id"] = self.project1.id
        session.save()
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 403)

    # 4. Synchronization Logic & Mocking
    @patch("oci_connector.services.sync_service.OCIClientFactory")
    def test_complete_sync_mapping(self, MockFactory):
        mock_factory = MagicMock()
        MockFactory.return_value = mock_factory

        mock_id = mock_factory.get_identity_client.return_value
        mock_root = MagicMock(id="c1")
        mock_root.name = "root"
        mock_id.get_compartment.return_value = MagicMock(data=mock_root)
        
        mock_child = MagicMock(id="c2", lifecycle_state="ACTIVE")
        mock_child.name = "child"
        mock_id.list_compartments.return_value = MagicMock(
            has_next_page=False,
            data=[mock_child]
        )
        mock_id.list_region_subscriptions.return_value = MagicMock(
            has_next_page=False,
            data=[
                MagicMock(region_name="us-ashburn-1"),
                MagicMock(region_name="eu-frankfurt-1"),
            ]
        )

        mock_comp = mock_factory.get_compute_client.return_value
        vm1 = MagicMock(
            id="vm1",
            display_name="instance-1",
            lifecycle_state="RUNNING",
            shape="VM.Standard.E4.Flex",
            shape_config=MagicMock(ocpus=2.0, memory_in_gbs=16.0),
        )
        mock_comp.list_instances.return_value = MagicMock(has_next_page=False, data=[vm1])

        mock_block = mock_factory.get_blockstorage_client.return_value
        vol1 = MagicMock(id="vol1", display_name="volume-1", lifecycle_state="AVAILABLE", size_in_gbs=50)
        boot1 = MagicMock(id="boot1", display_name="boot-1", lifecycle_state="AVAILABLE", size_in_gbs=46)
        mock_block.list_volumes.return_value = MagicMock(has_next_page=False, data=[vol1])
        mock_block.list_boot_volumes.return_value = MagicMock(has_next_page=False, data=[boot1])
        
        att1 = MagicMock(volume_id="vol1", instance_id="vm1", lifecycle_state="ATTACHED")
        mock_comp.list_volume_attachments.return_value = MagicMock(has_next_page=False, data=[att1])

        mock_net = mock_factory.get_virtual_network_client.return_value
        ip1 = MagicMock(
            id="ip1",
            ip_address="129.146.1.1",
            scope="REGION",
            lifecycle_state="AVAILABLE",
            assigned_entity_type=None,
            assigned_entity_id=None,
        )
        mock_net.list_public_ips.return_value = MagicMock(has_next_page=False, data=[ip1])

        mock_lb = mock_factory.get_load_balancer_client.return_value
        lb1 = MagicMock(
            id="lb1",
            display_name="lb-1",
            shape_name="flexible",
            lifecycle_state="ACTIVE",
            is_private=False,
            ip_addresses=[MagicMock(ip_address="129.146.2.2")],
        )
        mock_lb.list_load_balancers.return_value = MagicMock(has_next_page=False, data=[lb1])

        mock_os = mock_factory.get_object_storage_client.return_value
        mock_os.get_namespace.return_value = MagicMock(data="test-namespace")
        b1 = MagicMock()
        b1.name = "bucket-1"
        mock_os.list_buckets.return_value = MagicMock(has_next_page=False, data=[b1])
        mock_os.get_bucket.return_value = MagicMock(
            data=MagicMock(approximate_size=102400, approximate_count=50, storage_tier="Standard")
        )

        mock_usage = mock_factory.get_usage_client.return_value
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today - datetime.timedelta(days=1)
        cost_row = MagicMock(
            resource_id="vm1",
            resource_name="instance-1",
            service="Compute",
            compartment_id="c1",
            region="us-ashburn-1",
            availability_domain="AD-1",
            unit="OCPU-Hours",
            amount=5.25,
            currency="USD",
            qty=24.0,
            time_usage_started=yesterday,
            time_usage_ended=today,
        )
        mock_usage.request_summarized_usages.return_value = MagicMock(has_next_page=False, data=[cost_row])

        mock_mon = mock_factory.get_monitoring_client.return_value
        metric_data = MagicMock(
            dimensions={"resourceId": "vm1"},
            datapoints=[MagicMock(value=4.5, timestamp=yesterday)]
        )
        mock_mon.summarize_metrics_data.return_value = MagicMock(data=[metric_data])

        # Execute Sync Service
        sync_service = OCISyncService(self.connection)
        sync_service.factory = mock_factory
        sync_service.sync_all()

        log = OCISyncLog.objects.filter(project=self.project1).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, "COMPLETED")

        vm_db = OCIComputeInstance.objects.get(project=self.project1, ocid="vm1")
        self.assertEqual(vm_db.name, "instance-1")
        self.assertEqual(vm_db.state, "RUNNING")
        self.assertEqual(vm_db.ocpus, Decimal("2.00"))

        vol_db = OCIVolume.objects.get(project=self.project1, ocid="vol1")
        self.assertEqual(vol_db.attachment_state, "ATTACHED")
        self.assertEqual(vol_db.attached_instance_id, "vm1")

        boot_db = OCIVolume.objects.get(project=self.project1, ocid="boot1")
        self.assertEqual(boot_db.attachment_state, "DETACHED")

        ip_db = OCIPublicIp.objects.get(project=self.project1, ocid="ip1")
        self.assertEqual(ip_db.ip_address, "129.146.1.1")
        self.assertTrue(ip_db.is_orphan)

        lb_db = OCILoadBalancer.objects.get(project=self.project1, ocid="lb1")
        self.assertEqual(lb_db.ip_addresses, ["129.146.2.2"])

        b_db = OCIObjectStorageBucket.objects.get(project=self.project1, name="bucket-1")
        self.assertEqual(b_db.approximate_count, 50)

        upload = BillingUpload.objects.get(project=self.project1, upload_type="OCI API Sync")
        self.assertEqual(upload.upload_status, "Completed")
        record = BillingRecord.objects.get(upload=upload)
        self.assertEqual(record.resource_id, "vm1")
        self.assertEqual(record.cost, Decimal("5.25"))
        self.assertIsNotNone(record.source_fingerprint)

        metric = OCIResourceMetricSummary.objects.filter(resource_id="vm1").first()
        self.assertIsNotNone(metric)
        self.assertEqual(metric.average_value, Decimal("4.5000"))

    # 5. Cost Sync Idempotency and Reconciliation
    @patch("oci_connector.services.sync_service.OCIClientFactory")
    def test_cost_sync_idempotency_and_reconciliation(self, MockFactory):
        mock_factory = MagicMock()
        MockFactory.return_value = mock_factory
        
        mock_usage = mock_factory.get_usage_client.return_value
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today - datetime.timedelta(days=1)
        
        cost_row = MagicMock(
            resource_id="vm1",
            resource_name="instance-1",
            service="Compute",
            compartment_id="c1",
            region="us-ashburn-1",
            availability_domain="AD-1",
            unit="OCPU-Hours",
            amount=5.25,
            currency="USD",
            qty=24.0,
            time_usage_started=yesterday,
            time_usage_ended=today,
        )
        mock_usage.request_summarized_usages.return_value = MagicMock(has_next_page=False, data=[cost_row])

        # Run first Sync
        sync_service = OCISyncService(self.connection)
        sync_service.factory = mock_factory
        sync_service.sync_cost_data(days=2)
        
        records_cnt1 = BillingRecord.objects.filter(upload__project=self.project1, upload__upload_type="OCI API Sync").count()
        self.assertEqual(records_cnt1, 1)

        # Run identical second Sync (No duplicates created)
        sync_service.sync_cost_data(days=2)
        records_cnt2 = MagicMock() # we can just query database count
        records_cnt2 = BillingRecord.objects.filter(upload__project=self.project1, upload__upload_type="OCI API Sync").count()
        self.assertEqual(records_cnt2, 1)

        # Run sync with corrected cost: updates existing record in place
        cost_row.amount = 6.50
        sync_service.sync_cost_data(days=2)
        
        rec = BillingRecord.objects.get(upload__project=self.project1, upload__upload_type="OCI API Sync")
        self.assertEqual(rec.cost, Decimal("6.50"))

    def test_csv_reconciliation_safety(self):
        # Ensure OCI reconciliation NEVER modifies CSV billing records
        csv_upload = BillingUpload.objects.create(
            project=self.project1,
            upload_type="CSV Upload",
            title="CSV Upload",
            upload_status="Completed"
        )
        csv_rec = BillingRecord.objects.create(
            upload=csv_upload,
            service="Compute",
            resource_id="vm1",
            resource_name="instance-1",
            cost=Decimal("100.00"),
            amount=Decimal("100.00"),
            currency="USD",
            usage_start=timezone.now() - datetime.timedelta(days=1),
            usage_end=timezone.now(),
            source_fingerprint="some-fp"
        )

        # Run cost sync and ensure it doesn't touch csv_rec
        mock_factory = MagicMock()
        mock_usage = mock_factory.get_usage_client.return_value
        cost_row = MagicMock(
            resource_id="vm1",
            resource_name="instance-1",
            service="Compute",
            compartment_id="c1",
            region="us-ashburn-1",
            availability_domain="AD-1",
            unit="OCPU-Hours",
            amount=105.00,
            currency="USD",
            qty=24.0,
            time_usage_started=timezone.now() - datetime.timedelta(days=1),
            time_usage_ended=timezone.now(),
        )
        mock_usage.request_summarized_usages.return_value = MagicMock(has_next_page=False, data=[cost_row])

        sync_service = OCISyncService(self.connection)
        sync_service.factory = mock_factory
        sync_service.sync_cost_data(days=2)

        csv_rec.refresh_from_db()
        self.assertEqual(csv_rec.cost, Decimal("100.00")) # CSV record must remain untouched!
        self.assertEqual(BillingRecord.objects.filter(upload__upload_type="OCI API Sync").count(), 1)

    # 6. Concurrency Protection
    @patch("oci_connector.services.sync_service.OCIClientFactory")
    def test_concurrent_sync_protection(self, MockFactory):
        log = OCISyncLog.objects.create(
            project=self.project1,
            connection=self.connection,
            sync_type="ALL",
            status="PROCESSING"
        )
        
        sync_service = OCISyncService(self.connection)
        sync_service.factory = MockFactory.return_value
        with self.assertRaises(ValidationError):
            sync_service.start_log("ALL")

    # 7. Authoritative Resource Absence handling
    @patch("oci_connector.services.sync_service.OCIClientFactory")
    def test_authoritative_absence_handling(self, MockFactory):
        prev_seen = timezone.now() - datetime.timedelta(minutes=10)
        vm = OCIComputeInstance.objects.create(
            project=self.project1,
            connection=self.connection,
            ocid="vm-old",
            name="old-vm",
            state="RUNNING",
            shape="VM.Standard.E4.Flex",
            region="us-ashburn-1",
            compartment_id="c1",
            last_seen_at=prev_seen,
            inventory_status="PRESENT"
        )

        sync_service = OCISyncService(self.connection)
        sync_service.factory = MockFactory.return_value
        
        sync_service.success_scopes = {
            "compute": [("us-ashburn-1", "c1")],
            "volume": [],
            "bucket": [],
            "public_ip": [],
            "load_balancer": [],
        }
        sync_service.clean_absent_resources()
        
        vm.refresh_from_db()
        self.assertEqual(vm.inventory_status, "ABSENT")
        self.assertEqual(vm.state, "RUNNING") # Lifecycle state is preserved!

    # 8. Waste Detection upgraded with OCI evidence
    def test_telemetry_enhanced_waste_detection(self):
        OCIVolume.objects.create(
            project=self.project1,
            connection=self.connection,
            ocid="vol-detached",
            name="detached-vol",
            volume_type="BLOCK",
            state="AVAILABLE",
            size_in_gbs=100,
            attachment_state="DETACHED",
            inventory_status="PRESENT",
            region="us-ashburn-1",
            compartment_id="c1",
        )

        results = run_waste_detection_for_project(self.project1)
        
        finding = WasteFinding.objects.get(project=self.project1, waste_type="DETACHED_VOLUME")
        self.assertEqual(finding.confidence, "HIGH")
        self.assertIn("observed the volume without an attachment", finding.evidence)
        self.assertEqual(finding.estimated_monthly_savings, Decimal("5.00"))

    # 9. Recommendation upgrade with OCI evidence
    def test_recommendation_upgrade_with_telemetry(self):
        wf = WasteFinding.objects.create(
            project=self.project1,
            waste_type="IDLE_COMPUTE_CANDIDATE",
            resource_key="id:vm-idle",
            resource_id="vm-idle",
            resource_name="idle-vm",
            service_name="Compute",
            region="us-ashburn-1",
            currency="USD",
            first_seen=datetime.date.today(),
            last_seen=datetime.date.today(),
            observation_days=7,
            calendar_span_days=7,
            coverage_ratio=Decimal("1.0"),
            total_cost=Decimal("20.00"),
            average_daily_cost=Decimal("0.66"),
            estimated_monthly_cost=Decimal("20.00"),
            estimated_monthly_savings=Decimal("20.00"),
            confidence="HIGH",
            evidence="OCI monitoring details show CPU < 5%",
            status="OPEN"
        )

        run_recommendation_engine_for_project(self.project1)
        
        rec = wf.project.recommendations.filter(source_type="WASTE_FINDING", source_id=wf.id).first()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.confidence, "HIGH")
        self.assertIn("OCI monitoring data confirms low observed CPU activity", rec.limitations)

    # 10. Gemini serializer boundaries
    def test_gemini_serializer_exclusions(self):
        anomaly = MagicMock(
            anomaly_type="DAILY_SPIKE",
            detected_date=datetime.date.today(),
            service_name="Compute",
            resource_id="vm1",
            resource_name="vm-1",
            region="us-ashburn-1",
            actual_cost=Decimal("150.00"),
            expected_cost=Decimal("50.00"),
            deviation_percentage=Decimal("200.0"),
            z_score=3.5,
            severity="HIGH",
            description="Spike detected",
        )
        data = get_anomaly_deterministic_data(anomaly)
        self.assertNotIn("private_key", data)
        self.assertNotIn("tenancy_ocid", data)
        self.assertNotIn("user_ocid", data)

        wf = MagicMock(
            waste_type="IDLE_COMPUTE_CANDIDATE",
            resource_id="vm1",
            resource_name="vm-1",
            service_name="Compute",
            region="us-ashburn-1",
            currency="USD",
            first_seen=datetime.date.today(),
            last_seen=datetime.date.today(),
            observation_days=7,
            calendar_span_days=7,
            coverage_ratio=1.0,
            total_cost=Decimal("100.00"),
            average_daily_cost=Decimal("14.00"),
            estimated_monthly_cost=Decimal("420.00"),
            estimated_monthly_savings=Decimal("200.00"),
            confidence="HIGH",
            evidence="Idle CPU",
        )
        wf_data = get_waste_deterministic_data(wf)
        self.assertNotIn("private_key", wf_data)
        self.assertNotIn("tenancy_ocid", wf_data)

    def test_encryption_no_secret_key_fallback(self):
        # Validate that get_encryption_key fails closed without settings.SECRET_KEY fallback
        if "OCI_ENCRYPTION_KEY" in os.environ:
            del os.environ["OCI_ENCRYPTION_KEY"]
        with self.assertRaises(ImproperlyConfigured):
            get_encryption_key()

    @patch("oci_connector.services.sync_service.OCIClientFactory")
    def test_pagination_multiple_pages(self, MockFactory):
        # Check that page 1 and page 2 are both consumed
        mock_factory = MagicMock()
        compute_client = mock_factory.get_compute_client.return_value
        
        p1 = MagicMock(id="vm1", display_name="vm-1", lifecycle_state="RUNNING", shape="VM", shape_config=None)
        p2 = MagicMock(id="vm2", display_name="vm-2", lifecycle_state="RUNNING", shape="VM", shape_config=None)
        
        response1 = MagicMock(has_next_page=True, next_page="token", data=[p1])
        response2 = MagicMock(has_next_page=False, data=[p2])
        
        compute_client.list_instances.side_effect = [response1, response2]
        
        from oci_connector.services.sync_service import paginate_oci_call
        results = paginate_oci_call(compute_client.list_instances)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].id, "vm1")
        self.assertEqual(results[1].id, "vm2")

    @patch("oci_connector.services.sync_service.OCIClientFactory")
    def test_failed_pagination_prevents_absence_cleanup(self, MockFactory):
        # Check that a failed pagination call aborts absence cleanup for that scope
        vm = OCIComputeInstance.objects.create(
            project=self.project1,
            connection=self.connection,
            ocid="vm-old",
            name="old-vm",
            state="RUNNING",
            shape="VM",
            region="us-ashburn-1",
            compartment_id="c1",
            last_seen_at=timezone.now() - datetime.timedelta(minutes=10),
            inventory_status="PRESENT"
        )
        
        sync_service = OCISyncService(self.connection)
        sync_service.factory = MockFactory.return_value
        compute_client = sync_service.factory.get_compute_client.return_value
        
        # Page 2 fails
        response1 = MagicMock(has_next_page=True, next_page="token", data=[])
        compute_client.list_instances.side_effect = [response1, Exception("Pagination error")]
        
        compartments = [MagicMock(id="c1", lifecycle_state="ACTIVE", name="c1")]
        regions = ["us-ashburn-1"]
        
        sync_service.sync_inventory_data(compartments, regions)
        
        # vm should NOT be marked ABSENT
        vm.refresh_from_db()
        self.assertEqual(vm.inventory_status, "PRESENT")

    def test_volume_attachment_unknown_handling(self):
        # If attachment discovery fails, volume attachment_state must remain UNKNOWN
        # and unattached volume finding must not be generated
        mock_factory = MagicMock()
        sync_service = OCISyncService(self.connection)
        sync_service.factory = mock_factory
        
        vol = MagicMock(id="vol1", display_name="vol-1", lifecycle_state="AVAILABLE", size_in_gbs=100)
        
        # Simulate attachments discovery failure
        sync_service.process_volumes([vol], [], [], False, "us-ashburn-1", "c1")
        
        db_vol = OCIVolume.objects.get(project=self.project1, ocid="vol1")
        self.assertEqual(db_vol.attachment_state, "UNKNOWN")
        
        # Run waste detection, should not flag DETACHED_VOLUME
        results = run_waste_detection_for_project(self.project1)
        self.assertEqual(WasteFinding.objects.filter(project=self.project1, waste_type="DETACHED_VOLUME").count(), 0)

    def test_telemetry_metrics_missing_and_coverage(self):
        # 1. Expected daily samples comes from registry
        from oci_connector.services.sync_service import METRIC_REGISTRY
        expected = METRIC_REGISTRY["CPU_UTILIZATION"]["expected_daily_samples"]
        self.assertEqual(expected, 24)
        
        # 2. Decimal coverage ratios and missing value remain None
        mock_factory = MagicMock()
        sync_service = OCISyncService(self.connection)
        sync_service.factory = mock_factory
        
        monitoring_client = mock_factory.get_monitoring_client.return_value
        
        # 6 data points -> 6/24 = 0.25 coverage
        dp = [MagicMock(value=10.0, timestamp=timezone.now()) for _ in range(6)]
        mock_data = MagicMock(dimensions={"resourceId": "vm1"}, datapoints=dp)
        monitoring_client.summarize_metrics_data.return_value = MagicMock(data=[mock_data])
        
        compartments = [MagicMock(id="c1", lifecycle_state="ACTIVE", name="c1")]
        regions = ["us-ashburn-1"]
        
        sync_service.sync_metrics_data(compartments, regions)
        
        metric = OCIResourceMetricSummary.objects.get(resource_id="vm1", metric_name="CpuUtilization")
        self.assertEqual(metric.coverage_ratio, Decimal("0.25"))
        self.assertEqual(metric.average_value, Decimal("10.00"))

    def test_project_isolation_boundaries(self):
        # 1. Project A cannot access Project B connection
        self.client.force_login(self.unrelated)
        session = self.client.session
        session["active_project_id"] = self.project2.id
        session.save()
        
        # Request Project A's connection via AJAX connection test (IDOR check)
        url = reverse("oci_connector:test-connection")
        response = self.client.post(url)
        # Should return 404 since connection 1 is project1 scoped
        self.assertEqual(response.status_code, 404)


@override_settings(OCI_ENCRYPTION_KEY="MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=")
class Module15DeploymentTests(TestCase):
    def setUp(self):
        self.org1 = Organization.objects.create(name="Org A", slug="org-a")
        self.org2 = Organization.objects.create(name="Org B", slug="org-b")
        self.project1 = Project.objects.create(organization=self.org1, name="Project A", slug="project-a")
        self.connection1 = OCIConnection.objects.create(
            project=self.project1,
            name="OCI Conn 1",
            is_active=True
        )
        self.project2 = Project.objects.create(organization=self.org2, name="Project B", slug="project-b")
        self.connection2 = OCIConnection.objects.create(
            project=self.project2,
            name="OCI Conn 2",
            is_active=False
        )

    def test_health_check_liveness(self):
        url = reverse("health-check")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        # Verify no database internals or sensitive settings are leaked
        self.assertNotIn("database", response.json())
        self.assertNotIn("SECRET_KEY", response.json())

    def test_readiness_check_success(self):
        url = reverse("readiness-check")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})

    @patch("django.db.backends.utils.CursorWrapper.execute")
    def test_readiness_check_database_failure(self, mock_execute):
        # Force database OperationalError
        from django.db.utils import OperationalError
        mock_execute.side_effect = OperationalError("Connection refused")
        url = reverse("readiness-check")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        # Verify no stack trace or internal database error message is leaked
        self.assertNotIn("Connection refused", response.content.decode())

    def run_settings_in_subprocess(self, env_vars):
        import subprocess
        import sys
        import os
        from django.conf import settings
        env = os.environ.copy()
        keys_to_clear = [
            "SECRET_KEY", "DEBUG", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS",
            "DB_ENGINE", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT",
            "OCI_ENCRYPTION_KEY", "GEMINI_API_KEY", "GEMINI_MODEL"
        ]
        for k in keys_to_clear:
            env.pop(k, None)
        env.update(env_vars)
        env_file = os.path.join(settings.BASE_DIR, ".env")
        env_bak = os.path.join(settings.BASE_DIR, ".env.bak")
        has_env = os.path.exists(env_file)
        if has_env:
            os.rename(env_file, env_bak)
        code = (
            "import os, sys\n"
            "from django.core.exceptions import ImproperlyConfigured\n"
            "try:\n"
            "    import cloud_cost_detective.settings\n"
            "    print('SUCCESS')\n"
            "    sys.exit(0)\n"
            "except ImproperlyConfigured as e:\n"
            "    print('IMPROPERLY_CONFIGURED:', str(e))\n"
            "    sys.exit(10)\n"
            "except Exception as e:\n"
            "    print('OTHER_ERROR:', type(e).__name__, str(e))\n"
            "    sys.exit(20)\n"
        )
        try:
            res = subprocess.run(
                [sys.executable, "-c", code],
                env=env,
                capture_output=True,
                text=True,
                cwd=str(settings.BASE_DIR)
            )
        finally:
            if has_env:
                os.rename(env_bak, env_file)
        return res.returncode, res.stdout, res.stderr

    def test_production_settings_reject_missing_secret_key(self):
        env = {
            "DEBUG": "False",
            "SECRET_KEY": "",
            "ALLOWED_HOSTS": "localhost,127.0.0.1",
            "DB_ENGINE": "django.db.backends.postgresql",
            "DB_NAME": "proddb",
            "DB_USER": "produser",
            "DB_PASSWORD": "prodpassword",
            "DB_HOST": "localhost",
            "DB_PORT": "5432",
            "OCI_ENCRYPTION_KEY": "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=",
        }
        code, stdout, stderr = self.run_settings_in_subprocess(env)
        self.assertEqual(code, 10)
        self.assertIn("SECRET_KEY must be explicitly configured in production.", stdout)

    def test_production_settings_reject_default_fallback_secret_key(self):
        env = {
            "DEBUG": "False",
            "SECRET_KEY": "django-insecure-36m0b016f(m^5&0)s-@de=v&wmxwot^o%ts3!5f@fbbrw$jf8&",
            "ALLOWED_HOSTS": "localhost,127.0.0.1",
            "DB_ENGINE": "django.db.backends.postgresql",
            "DB_NAME": "proddb",
            "DB_USER": "produser",
            "DB_PASSWORD": "prodpassword",
            "DB_HOST": "localhost",
            "DB_PORT": "5432",
            "OCI_ENCRYPTION_KEY": "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=",
        }
        code, stdout, stderr = self.run_settings_in_subprocess(env)
        self.assertEqual(code, 10)
        self.assertIn("SECRET_KEY must be explicitly configured in production.", stdout)

    def test_production_settings_reject_missing_allowed_hosts(self):
        env = {
            "DEBUG": "False",
            "SECRET_KEY": "prod-explicit-secret-key-12345",
            "ALLOWED_HOSTS": "",
            "DB_ENGINE": "django.db.backends.postgresql",
            "DB_NAME": "proddb",
            "DB_USER": "produser",
            "DB_PASSWORD": "prodpassword",
            "DB_HOST": "localhost",
            "DB_PORT": "5432",
            "OCI_ENCRYPTION_KEY": "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=",
        }
        code, stdout, stderr = self.run_settings_in_subprocess(env)
        self.assertEqual(code, 10)
        self.assertIn("ALLOWED_HOSTS must be explicitly configured when DEBUG=False.", stdout)

    def test_production_settings_reject_incomplete_database(self):
        env = {
            "DEBUG": "False",
            "SECRET_KEY": "prod-explicit-secret-key-12345",
            "ALLOWED_HOSTS": "localhost,127.0.0.1",
            "DB_ENGINE": "django.db.backends.postgresql",
            "DB_NAME": "proddb",
            "DB_USER": "produser",
            "DB_PASSWORD": "",
            "DB_HOST": "localhost",
            "DB_PORT": "5432",
            "OCI_ENCRYPTION_KEY": "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=",
        }
        code, stdout, stderr = self.run_settings_in_subprocess(env)
        self.assertEqual(code, 10)
        self.assertIn("database setting 'PASSWORD' must be explicitly configured.", stdout)

    def test_production_settings_valid_production_environment(self):
        env = {
            "DEBUG": "False",
            "SECRET_KEY": "prod-explicit-secret-key-12345",
            "ALLOWED_HOSTS": "localhost,127.0.0.1",
            "DB_ENGINE": "django.db.backends.postgresql",
            "DB_NAME": "proddb",
            "DB_USER": "produser",
            "DB_PASSWORD": "prodpassword",
            "DB_HOST": "localhost",
            "DB_PORT": "5432",
            "OCI_ENCRYPTION_KEY": "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=",
        }
        code, stdout, stderr = self.run_settings_in_subprocess(env)
        self.assertEqual(code, 0, f"Stdout: {stdout}\nStderr: {stderr}")
        self.assertIn("SUCCESS", stdout)

    @patch("oci_connector.tasks.sync_oci_data_task.delay")
    def test_sync_all_active_connections_queues_only_active(self, mock_delay):
        from oci_connector.tasks import sync_all_active_connections_task
        sync_all_active_connections_task()
        mock_delay.assert_called_once_with(self.connection1.id)

    @patch("oci_connector.tasks.sync_oci_data_task.delay")
    def test_sync_all_active_connections_failure_isolation(self, mock_delay):
        from oci_connector.tasks import sync_all_active_connections_task
        # Clean up existing connections created in setUp to have full control
        OCIConnection.objects.all().delete()
        
        # Create three active connections with unique slugs
        orgA = Organization.objects.create(name="Org A Unique", slug="org-unique-a")
        projA = Project.objects.create(organization=orgA, name="Proj A", slug="proj-unique-a")
        connA = OCIConnection.objects.create(project=projA, name="Conn A", is_active=True)
        
        orgB = Organization.objects.create(name="Org B Unique", slug="org-unique-b")
        projB = Project.objects.create(organization=orgB, name="Proj B", slug="proj-unique-b")
        connB = OCIConnection.objects.create(project=projB, name="Conn B", is_active=True)
        
        orgC = Organization.objects.create(name="Org C Unique", slug="org-unique-c")
        projC = Project.objects.create(organization=orgC, name="Proj C", slug="proj-unique-c")
        connC = OCIConnection.objects.create(project=projC, name="Conn C", is_active=True)
        
        # Connection A enqueue raises exception, B succeeds, C succeeds
        mock_delay.side_effect = [
            Exception("Queue unavailable"),
            None,
            None,
        ]
        
        # Run periodic scheduler task
        sync_all_active_connections_task()
        
        # Verify enqueue was attempted for all three (total attempts = 3)
        self.assertEqual(mock_delay.call_count, 3)
        
        # Verify the actual connection IDs passed in order
        calls = [c[0][0] for c in mock_delay.call_args_list]
        self.assertIn(connA.id, calls)
        self.assertIn(connB.id, calls)
        self.assertIn(connC.id, calls)

    @patch("oci_connector.tasks.sync_oci_data_task.delay")
    @patch("oci_connector.services.oci_client.decrypt_private_key")
    def test_sync_all_active_connections_data_minimization(self, mock_decrypt, mock_delay):
        from oci_connector.tasks import sync_all_active_connections_task
        sync_all_active_connections_task()
        mock_delay.assert_called_once_with(self.connection1.id)
        mock_decrypt.assert_not_called()

    @patch("oci_connector.services.oci_client.decrypt_private_key")
    @patch("oci_connector.services.oci_client.sanitize_oci_error")
    def test_oci_logging_security_regression(self, mock_sanitize, mock_decrypt):
        # We inject a synthetic exception with obvious secret markers
        secret_marker = "SECRET_PRIVATE_KEY_MARKER_12345"
        password_marker = "OCI_PASSWORD_MARKER_abcde"
        endpoint_marker = "https://internal-oci-endpoint.example.com"
        raw_msg = f"Failed with {secret_marker} and {password_marker} on {endpoint_marker}"
        
        # Force decrypt_private_key to raise an expected InvalidToken cryptography exception
        from cryptography.fernet import InvalidToken
        mock_decrypt.side_effect = InvalidToken(raw_msg)
        # Mock sanitize_oci_error to return a safe static message
        mock_sanitize.return_value = "OCI authentication or configuration validation failed."
        
        from oci_connector.services.oci_client import build_oci_config
        # We create a dummy connection
        conn = OCIConnection(
            project=self.project1,
            name="Log Test Conn",
            private_key_encrypted="encrypted-payload-here"
        )
        
        with self.assertLogs("oci_connector.services.oci_client", level="ERROR") as cm:
            with self.assertRaises(ValidationError):
                build_oci_config(conn)
                
            # Verify the logged output does NOT contain the secret/password/endpoint markers
            log_output = "\n".join(cm.output)
            self.assertNotIn(secret_marker, log_output)
            self.assertNotIn(password_marker, log_output)
            self.assertNotIn(endpoint_marker, log_output)
            # Verify safe generic message is logged
            self.assertIn("OCI credential decryption failed.", log_output)

        # Also verify that when building config validate fails, it is sanitized
        mock_decrypt.side_effect = None
        mock_decrypt.return_value = "decrypted-key"
        
        with patch("oci.config.validate_config") as mock_validate:
            from oci.exceptions import InvalidConfig
            mock_validate.side_effect = InvalidConfig(raw_msg)
            
            with self.assertLogs("oci_connector.services.oci_client", level="ERROR") as cm:
                with self.assertRaises(ValidationError):
                    build_oci_config(conn)
                    
                log_output = "\n".join(cm.output)
                self.assertNotIn(secret_marker, log_output)
                self.assertNotIn(password_marker, log_output)
                self.assertNotIn(endpoint_marker, log_output)
                self.assertIn("OCI authentication or configuration validation failed.", log_output)

    def test_sync_service_fatal_error_logging_security(self):
        secret_marker = "SECRET_PRIVATE_KEY_MARKER_12345"
        password_marker = "OCI_PASSWORD_MARKER_abcde"
        endpoint_marker = "https://internal-oci-endpoint.example.com"
        raw_msg = f"Failed with {secret_marker} and {password_marker} on {endpoint_marker}"
        
        # Build sync service by mocking build_oci_config to return dummy dict
        with patch("oci_connector.services.sync_service.build_oci_config") as mock_build_config:
            mock_build_config.return_value = {"user": "ocid1.user.oc1..test"}
            sync_service = OCISyncService(self.connection1)
        
        # Trigger fatal error in sync_all during discover_compartments
        with patch.object(sync_service, "discover_compartments") as mock_discover:
            mock_discover.side_effect = Exception(raw_msg)
            sync_service.sync_all()
        
        # Get sync log
        sync_log = OCISyncLog.objects.filter(connection=self.connection1).latest("started_at")
        self.assertEqual(sync_log.status, "FAILED")
        
        # Verify the database log error summary does NOT contain the secret markers
        self.assertNotIn(secret_marker, sync_log.error_summary)
        self.assertNotIn(password_marker, sync_log.error_summary)
        self.assertNotIn(endpoint_marker, sync_log.error_summary)
        self.assertIn("OCI synchronization failed due to an internal application error.", sync_log.error_summary)

    @patch("oci_connector.views.test_oci_connection_stages")
    def test_ajax_view_exception_logging_security(self, mock_stages):
        secret_marker = "SECRET_PRIVATE_KEY_MARKER_12345"
        password_marker = "OCI_PASSWORD_MARKER_abcde"
        endpoint_marker = "https://internal-oci-endpoint.example.com"
        raw_msg = f"Failed with {secret_marker} and {password_marker} on {endpoint_marker}"
        
        # Mock test_oci_connection_stages to raise exception
        mock_stages.side_effect = Exception(raw_msg)
        
        # Create a test admin user dynamically
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.create_user(username="test_admin", email="test_admin@org1.com", password="pwd")
        from accounts.models import OrganizationMembership
        OrganizationMembership.objects.create(user=user, organization=self.org1, role="ADMIN")
        
        self.client.force_login(user)
        session = self.client.session
        session["active_project_id"] = self.project1.id
        session.save()
        
        # Send post request to test connection endpoint
        url = reverse("oci_connector:test-connection")
        response = self.client.post(url)
        
        self.assertEqual(response.status_code, 200) # Returns JSON response success=False
        resp_json = response.json()
        self.assertFalse(resp_json["success"])
        self.assertIn("error", resp_json)
        
        # Verify secret markers do not appear in HTTP response body
        response_text = response.content.decode()
        self.assertNotIn(secret_marker, response_text)
        self.assertNotIn(password_marker, response_text)
        self.assertNotIn(endpoint_marker, response_text)
        self.assertEqual(resp_json["error"], "Unable to complete the OCI operation. Please try again or review the connection configuration.")

    @patch("oci_connector.services.oci_client.decrypt_private_key")
    def test_oci_service_error_logging_security(self, mock_decrypt):
        secret_marker = "SECRET_PRIVATE_KEY_MARKER_12345"
        password_marker = "OCI_PASSWORD_MARKER_abcde"
        endpoint_marker = "https://internal-oci-endpoint.example.com"
        
        # Build a synthetic ServiceError
        from oci.exceptions import ServiceError
        # ServiceError constructor parameters: status, code, message, headers, request_id
        raw_msg = f"Failed with {secret_marker} and {password_marker} on {endpoint_marker}"
        service_error = ServiceError(
            status=403,
            code="NotAuthorizedOrNotFound",
            message=raw_msg,
            headers={"opc-request-id": "12345"},
            request_id="12345"
        )
        
        # Force build_oci_config to raise this ServiceError during validation
        mock_decrypt.return_value = "decrypted-key"
        
        with patch("oci.config.validate_config") as mock_validate:
            mock_validate.side_effect = service_error
            
            from oci_connector.services.oci_client import build_oci_config
            conn = OCIConnection(
                project=self.project1,
                name="Log Test Conn 2",
                private_key_encrypted="encrypted-payload"
            )
            
            with self.assertLogs("oci_connector.services.oci_client", level="ERROR") as cm:
                with self.assertRaises(ValidationError):
                    build_oci_config(conn)
                
                log_output = "\n".join(cm.output)
                # Verify logger.error was called with the sanitized message
                self.assertIn("OCI authentication or authorization failed.", log_output)
                # Verify raw synthetic sensitive values are not in logs
                self.assertNotIn(secret_marker, log_output)
                self.assertNotIn(password_marker, log_output)
                self.assertNotIn(endpoint_marker, log_output)

    @patch("oci_connector.services.oci_client.decrypt_private_key")
    def test_oci_network_timeout_logging_security(self, mock_decrypt):
        secret_marker = "SECRET_PRIVATE_KEY_MARKER_12345"
        password_marker = "OCI_PASSWORD_MARKER_abcde"
        endpoint_marker = "https://internal-oci-endpoint.example.com"
        
        import requests.exceptions
        raw_msg = f"Failed with {secret_marker} and {password_marker} on {endpoint_marker}"
        timeout_error = requests.exceptions.Timeout(raw_msg)
        
        mock_decrypt.return_value = "decrypted-key"
        
        with patch("oci.config.validate_config") as mock_validate:
            mock_validate.side_effect = timeout_error
            
            from oci_connector.services.oci_client import build_oci_config
            conn = OCIConnection(
                project=self.project1,
                name="Log Test Conn 3",
                private_key_encrypted="encrypted-payload"
            )
            
            with self.assertLogs("oci_connector.services.oci_client", level="ERROR") as cm:
                with self.assertRaises(ValidationError):
                    build_oci_config(conn)
                
                log_output = "\n".join(cm.output)
                # Verify logger.error was used with sanitized message
                self.assertIn("OCI request timed out or could not reach the service.", log_output)
                # Verify raw synthetic timeout payload is not exposed
                self.assertNotIn(secret_marker, log_output)
                self.assertNotIn(password_marker, log_output)
                self.assertNotIn(endpoint_marker, log_output)

