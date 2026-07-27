import hashlib
import warnings
import datetime
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, Project
from accounts.permissions import get_active_project
from accounts.services.tenant_service import provision_default_tenant
from billing.models import BillingUpload, BillingRecord
from ai_engine.models import Recommendation
from ai_engine.services.chat.intent_schema import ChatQueryPlan, IntentEnum, TimeRangeSchema, TimeRangeTypeEnum, QueryFiltersSchema
from ai_engine.services.chat.query_executor import execute_query_plan_for_project

User = get_user_model()

class Module13RegressionTestCase(TestCase):
    def setUp(self):
        # Create users
        self.owner_user = User.objects.create_user(username="owner_user", email="owner@example.com", password="password")
        self.admin_user = User.objects.create_user(username="admin_user", email="admin@example.com", password="password")
        self.viewer_user = User.objects.create_user(username="viewer_user", email="viewer@example.com", password="password")
        
        # Clean auto-created tenants to have precise control
        OrganizationMembership.objects.all().delete()
        Project.objects.all().delete()
        Organization.objects.all().delete()
        
        # Setup Org 1
        self.org1 = Organization.objects.create(name="Org 1", slug="org-1")
        self.project1_a = Project.objects.create(name="Project 1A", slug="project-1a", organization=self.org1)
        self.project1_b = Project.objects.create(name="Project 1B", slug="project-1b", organization=self.org1)
        
        self.owner_membership = OrganizationMembership.objects.create(user=self.owner_user, organization=self.org1, role="OWNER")
        self.admin_membership = OrganizationMembership.objects.create(user=self.admin_user, organization=self.org1, role="ADMIN")
        self.viewer_membership = OrganizationMembership.objects.create(user=self.viewer_user, organization=self.org1, role="VIEWER")
        
        # Setup Org 2 (completely separate organization for cross-project tests)
        self.org2 = Organization.objects.create(name="Org 2", slug="org-2")
        self.project2 = Project.objects.create(name="Project 2", slug="project-2", organization=self.org2)
        
        # Billing records in Project 1A
        self.upload_1a = BillingUpload.objects.create(project=self.project1_a, original_filename="u1a.csv", uploaded_by=self.owner_user)
        BillingRecord.objects.create(
            upload=self.upload_1a, service="Compute", resource_id="res-1a",
            compartment="comp1", region="us-ashburn-1", usage_start=datetime.datetime(2026, 6, 15, 0, 0, tzinfo=datetime.timezone.utc), cost=150.00
        )
        # Billing records in Project 1B
        self.upload_1b = BillingUpload.objects.create(project=self.project1_b, original_filename="u1b.csv", uploaded_by=self.owner_user)
        BillingRecord.objects.create(
            upload=self.upload_1b, service="Compute", resource_id="res-1b",
            compartment="comp1", region="us-ashburn-1", usage_start=datetime.datetime(2026, 6, 15, 0, 0, tzinfo=datetime.timezone.utc), cost=300.00
        )

    def test_cost_increase_explanation_cannot_read_another_project(self):
        """COST_INCREASE_EXPLANATION must not leak records from other projects."""
        plan = ChatQueryPlan(
            intent=IntentEnum.COST_INCREASE_EXPLANATION,
            time_range=TimeRangeSchema(type=TimeRangeTypeEnum.LAST_MONTH),
            filters=QueryFiltersSchema()
        )
        # Query for Project 1A (should only see records in project 1a)
        res = execute_query_plan_for_project(self.project1_a, self.owner_user, plan)
        # Project 1A has a total cost of 150. USD
        self.assertEqual(res["currency_comparisons"][0]["current_total"], "150.00")

    def test_admin_cannot_promote_or_add_owner_manually(self):
        """ADMIN must not be allowed to promote or invite someone as OWNER."""
        self.client.login(username="admin_user", password="password")
        
        # Set active project
        session = self.client.session
        session["active_project_id"] = self.project1_a.id
        session.save()
        
        # Try to invite viewer as OWNER
        invite_url = reverse("accounts:org-members")
        response = self.client.post(invite_url, {
            "email": "viewer@example.com",
            "role": "OWNER"
        })
        self.assertEqual(response.status_code, 403)
        
        # Try to promote viewer to OWNER
        update_url = reverse("accounts:update-member-role", kwargs={"membership_id": self.viewer_membership.id})
        response = self.client.post(update_url, {"role": "OWNER"})
        self.assertEqual(response.status_code, 403)

    def test_invalid_role_strings_are_rejected(self):
        """Invalid role strings must be rejected server-side."""
        self.client.login(username="owner_user", password="password")
        session = self.client.session
        session["active_project_id"] = self.project1_a.id
        session.save()
        
        # Invite with invalid role
        invite_url = reverse("accounts:org-members")
        response = self.client.post(invite_url, {
            "email": "viewer@example.com",
            "role": "SUPER_ADMIN"
        })
        self.assertEqual(response.status_code, 403)
        
        # Update with invalid role
        update_url = reverse("accounts:update-member-role", kwargs={"membership_id": self.viewer_membership.id})
        response = self.client.post(update_url, {"role": "SUPER_ADMIN"})
        self.assertEqual(response.status_code, 403)

    def test_final_owner_cannot_be_removed(self):
        """The final OWNER of the organization must not be allowed to be removed."""
        self.client.login(username="owner_user", password="password")
        session = self.client.session
        session["active_project_id"] = self.project1_a.id
        session.save()
        
        # Try to remove the final owner
        remove_url = reverse("accounts:remove-member", kwargs={"membership_id": self.owner_membership.id})
        response = self.client.post(remove_url)
        self.assertEqual(response.status_code, 403)

    def test_final_owner_cannot_be_demoted(self):
        """The final OWNER of the organization must not be allowed to be demoted."""
        self.client.login(username="owner_user", password="password")
        session = self.client.session
        session["active_project_id"] = self.project1_a.id
        session.save()
        
        # Try to demote the final owner
        update_url = reverse("accounts:update-member-role", kwargs={"membership_id": self.owner_membership.id})
        response = self.client.post(update_url, {"role": "ADMIN"})
        self.assertEqual(response.status_code, 403)

    def test_final_owner_cannot_leave_organization(self):
        """The final OWNER of the organization must not be allowed to leave (self-removal)."""
        self.client.login(username="owner_user", password="password")
        session = self.client.session
        session["active_project_id"] = self.project1_a.id
        session.save()
        
        # Self-removal endpoint (remove own membership)
        remove_url = reverse("accounts:remove-member", kwargs={"membership_id": self.owner_membership.id})
        response = self.client.post(remove_url)
        self.assertEqual(response.status_code, 403)

    def test_get_cannot_perform_member_mutations(self):
        """GET requests to mutation-only endpoints must return HTTP 405 Method Not Allowed."""
        self.client.login(username="owner_user", password="password")
        
        # Switch project GET
        response = self.client.get(reverse("accounts:switch-project"))
        self.assertEqual(response.status_code, 405)
        
        # Update member role GET
        update_url = reverse("accounts:update-member-role", kwargs={"membership_id": self.viewer_membership.id})
        response = self.client.get(update_url)
        self.assertEqual(response.status_code, 405)
        
        # Remove member GET
        remove_url = reverse("accounts:remove-member", kwargs={"membership_id": self.viewer_membership.id})
        response = self.client.get(remove_url)
        self.assertEqual(response.status_code, 405)

    def test_stale_active_project_clears_session_and_returns_none(self):
        """A stale or inaccessible project in session must not silently move user to fallback project."""
        self.client.login(username="owner_user", password="password")
        
        # Inject an invalid project ID (project2, which owner_user does not belong to) in request session
        session = self.client.session
        session["active_project_id"] = self.project2.id
        session.save()
        
        # Mock a request object
        class MockRequest:
            def __init__(self, user, session):
                self.user = user
                self.session = session
                
        req = MockRequest(self.owner_user, self.client.session)
        active_project = get_active_project(req)
        
        # Should clear session and return None
        self.assertIsNone(active_project)
        self.assertNotIn("active_project_id", req.session)

    def test_production_services_raise_deprecation_warnings(self):
        """Verify that legacy user-based wrappers are deprecated and warn."""
        from analytics.services.cost_forecaster import get_forecast_for_user
        from analytics.services.cost_simulator import run_cost_simulation
        from analytics.services.report_service import collect_report_data
        from analytics.services.recommendation_engine import run_recommendation_engine
        from ai_engine.services.chat.query_executor import execute_query_plan
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            
            # Call forecaster legacy wrapper
            get_forecast_for_user(self.owner_user)
            self.assertTrue(any(issubclass(warn.category, DeprecationWarning) for warn in w))
            
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            
            # Call cost simulator legacy wrapper
            try:
                run_cost_simulation(self.owner_user, "LAST_MONTH")
            except Exception:
                pass
            self.assertTrue(any(issubclass(warn.category, DeprecationWarning) for warn in w))
            
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            
            # Call report service legacy wrapper
            collect_report_data(self.owner_user, "LAST_MONTH")
            self.assertTrue(any(issubclass(warn.category, DeprecationWarning) for warn in w))

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            
            # Call recommendation engine legacy wrapper
            run_recommendation_engine(self.owner_user)
            self.assertTrue(any(issubclass(warn.category, DeprecationWarning) for warn in w))

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            
            # Call chat query legacy wrapper
            plan = ChatQueryPlan(
                intent=IntentEnum.TOTAL_COST,
                time_range=TimeRangeSchema(type=TimeRangeTypeEnum.LAST_MONTH),
                filters=QueryFiltersSchema()
            )
            execute_query_plan(self.owner_user, plan)
            self.assertTrue(any(issubclass(warn.category, DeprecationWarning) for warn in w))

    def test_recommendation_migration_handles_collisions_safely(self):
        """Verify that fingerprint backfill migration deletes duplicates to avoid IntegrityError."""
        # Create duplicate recommendations in Project 1A
        # In real migration, we use the raw models from apps.get_model
        # But we can test the behavior of the deduplication algorithm here.
        rec1 = Recommendation.objects.create(
            user=self.owner_user,
            project=self.project1_a,
            recommendation_type="RIGHTSIZE_REVIEW",
            source_type="WASTE_FINDING",
            identity_type="id",
            identity_value="res-1",
            fingerprint="fp-unique-1"
        )
        rec2 = Recommendation.objects.create(
            user=self.owner_user,
            project=self.project1_a,
            recommendation_type="RIGHTSIZE_REVIEW",
            source_type="WASTE_FINDING",
            identity_type="id",
            identity_value="res-1", # Same fields that build the raw_str
            fingerprint="fp-unique-2"
        )
        
        # Run our migration logic
        from django.apps import apps
        import importlib
        migration_module = importlib.import_module("accounts.migrations.0004_auto_20260726_2016")
        backfill_orgs_and_projects = migration_module.backfill_orgs_and_projects
        
        # Triggering the function should deduplicate and run successfully without IntegrityError
        # Mocking the apps container
        class MockApps:
            def get_model(self, app_label, model_name):
                return apps.get_model(app_label, model_name)
                
        # Running it should not fail
        backfill_orgs_and_projects(MockApps(), None)
        
        # Only one should survive
        recs = Recommendation.objects.filter(user=self.owner_user, recommendation_type="RIGHTSIZE_REVIEW")
        self.assertEqual(recs.count(), 1)

    def test_default_tenant_provisioning_does_not_duplicate(self):
        """Calling provision_default_tenant twice should not duplicate Organizations or Projects."""
        user = User.objects.create_user(username="temp_user", email="temp@example.com", password="password")
        
        # Clean auto-created tenant for temp_user to test service from clean state
        OrganizationMembership.objects.filter(user=user).delete()
        
        # Provision first time
        org1, membership1, proj1 = provision_default_tenant(user)
        self.assertIsNotNone(org1)
        
        # Provision second time
        org2, membership2, proj2 = provision_default_tenant(user)
        self.assertIsNone(org2) # Returns None, None, None because it detects existing membership
