from django.test import TestCase
from django.urls import reverse
from .models import User


class AuthenticationTests(TestCase):
    """Test case for the Module 1 Authentication System."""

    def setUp(self):
        self.register_url = reverse("register")
        self.login_url = reverse("login")
        self.logout_url = reverse("logout")
        self.profile_url = reverse("profile")
        self.dashboard_url = reverse("dashboard-home")

        self.user_data = {
            "username": "testuser",
            "email": "testuser@example.com",
            "password1": "testpassword123",
            "password2": "testpassword123",
            "organization": "Test Corp",
            "phone_number": "1234567890",
        }

    def test_user_registration_success(self):
        """Test successful registration with valid inputs."""
        response = self.client.post(self.register_url, {
            "username": "newuser",
            "email": "newuser@example.com",
            "password1": "newpassword123",
            "password2": "newpassword123",
            "organization": "New Org",
            "phone_number": "9876543210",
        })
        # Successful registration redirects to dashboard
        self.assertRedirects(response, self.dashboard_url)
        # Check database
        user = User.objects.get(username="newuser")
        self.assertEqual(user.email, "newuser@example.com")
        self.assertEqual(user.organization, "New Org")
        self.assertEqual(user.phone_number, "9876543210")

    def test_user_registration_validation(self):
        """Test registration validation errors (e.g. password mismatch, duplicate email)."""
        # Create an existing user to check duplicate email
        User.objects.create_user(username="existing", email="testuser@example.com", password="password")

        # 1. Password mismatch
        invalid_data = self.user_data.copy()
        invalid_data["password2"] = "mismatch"
        response = self.client.post(self.register_url, invalid_data)
        self.assertEqual(response.status_code, 200)  # Re-renders page
        self.assertFormError(response.context["form"], "password2", "The two password fields didn’t match.")

        # 2. Duplicate email
        invalid_data = self.user_data.copy()
        invalid_data["username"] = "anothername"
        response = self.client.post(self.register_url, invalid_data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "email", "A user with this email address already exists.")

    def test_login_success(self):
        """Test successful user login."""
        user = User.objects.create_user(username="testuser", email="test@example.com", password="testpassword123")
        response = self.client.post(self.login_url, {
            "username": "testuser",
            "password": "testpassword123",
        })
        self.assertRedirects(response, self.dashboard_url)
        # Verify session
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_login_invalid_credentials(self):
        """Test login failure with invalid credentials."""
        User.objects.create_user(username="testuser", email="test@example.com", password="testpassword123")
        response = self.client.post(self.login_url, {
            "username": "testuser",
            "password": "wrongpassword",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_logout(self):
        """Test user logout."""
        User.objects.create_user(username="testuser", email="test@example.com", password="testpassword123")
        self.client.login(username="testuser", password="testpassword123")
        response = self.client.get(self.logout_url)
        self.assertRedirects(response, self.login_url)
        # Verify session is logged out
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_login_required_restrictions(self):
        """Test that profile and dashboard pages are restricted to logged-in users."""
        # Accessing dashboard should redirect to login
        response = self.client.get(self.dashboard_url)
        self.assertRedirects(response, f"{self.login_url}?next={self.dashboard_url}")

        # Accessing profile should redirect to login
        response = self.client.get(self.profile_url)
        self.assertRedirects(response, f"{self.login_url}?next={self.profile_url}")

    def test_profile_update(self):
        """Test updating profile information."""
        user = User.objects.create_user(username="testuser", email="test@example.com", password="testpassword123")
        self.client.login(username="testuser", password="testpassword123")

        response = self.client.post(self.profile_url, {
            "first_name": "UpdatedFirst",
            "last_name": "UpdatedLast",
            "email": "updated@example.com",
            "organization": "Updated Corp",
            "phone_number": "5555555555",
        })
        self.assertRedirects(response, self.profile_url)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "UpdatedFirst")
        self.assertEqual(user.last_name, "UpdatedLast")
        self.assertEqual(user.email, "updated@example.com")
        self.assertEqual(user.organization, "Updated Corp")
        self.assertEqual(user.phone_number, "5555555555")


class MultiUserTeamFeaturesTests(TestCase):
    """Test case for the Module 13 Multi-user & Team Features (RBAC, Tenancy, IDOR, Project Switcher)."""

    def setUp(self):
        from accounts.models import Organization, OrganizationMembership, Project
        from billing.models import BillingUpload
        from analytics.models import CostAnomaly
        
        # Create users
        self.owner_user = User.objects.create_user(username="owner_user", email="owner@example.com", password="password")
        self.admin_user = User.objects.create_user(username="admin_user", email="admin@example.com", password="password")
        self.analyst_user = User.objects.create_user(username="analyst_user", email="analyst@example.com", password="password")
        self.viewer_user = User.objects.create_user(username="viewer_user", email="viewer@example.com", password="password")
        self.external_user = User.objects.create_user(username="external_user", email="external@example.com", password="password")
        
        # User signal automatically created default projects/orgs. Let's delete them to set up clean custom hierarchies for testing,
        # or we can use the ones automatically created.
        # Let's clean the auto-created objects to have precise control:
        OrganizationMembership.objects.all().delete()
        Project.objects.all().delete()
        Organization.objects.all().delete()

        # Create Org 1
        self.org1 = Organization.objects.create(name="Org 1", slug="org-1")
        self.project1_a = Project.objects.create(name="Project 1A", slug="project-1a", organization=self.org1)
        self.project1_b = Project.objects.create(name="Project 1B", slug="project-1b", organization=self.org1)
        
        # Assign roles in Org 1
        OrganizationMembership.objects.create(user=self.owner_user, organization=self.org1, role="OWNER")
        OrganizationMembership.objects.create(user=self.admin_user, organization=self.org1, role="ADMIN")
        OrganizationMembership.objects.create(user=self.analyst_user, organization=self.org1, role="ANALYST")
        OrganizationMembership.objects.create(user=self.viewer_user, organization=self.org1, role="VIEWER")
        
        # Create Org 2
        self.org2 = Organization.objects.create(name="Org 2", slug="org-2")
        self.project2 = Project.objects.create(name="Project 2", slug="project-2", organization=self.org2)
        OrganizationMembership.objects.create(user=self.external_user, organization=self.org2, role="OWNER")
        
        # Create data inside Project 1A
        self.upload_1a = BillingUpload.objects.create(
            project=self.project1_a,
            uploaded_by=self.owner_user,
            upload_type="Billing Report",
            original_filename="upload_1a.csv"
        )
        self.anomaly_1a = CostAnomaly.objects.create(
            project=self.project1_a,
            user=self.owner_user,
            anomaly_type="SPIKE",
            detected_date="2026-07-26",
            service_name="Compute",
            resource_id="res-1a",
            actual_cost=100.00,
            expected_cost=10.00,
            deviation_percentage=900.00
        )

        # Create data inside Project 2
        self.upload_2 = BillingUpload.objects.create(
            project=self.project2,
            uploaded_by=self.external_user,
            upload_type="Billing Report",
            original_filename="upload_2.csv"
        )

    def test_tenancy_data_isolation(self):
        """Verify that users can only see data belonging to their active project."""
        from billing.models import BillingUpload
        
        # Log in as Owner user (belongs to Org 1, default active project should fall back to project1_a)
        self.client.login(username="owner_user", password="password")
        
        # Set active project in session to project1_a
        session = self.client.session
        session["active_project_id"] = self.project1_a.id
        session.save()
        
        # Fetch uploads history view
        response = self.client.get(reverse("upload-history"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "upload_1a.csv")
        self.assertNotContains(response, "upload_2.csv")
        
        # Switch to project1_b (should see empty history)
        session = self.client.session
        session["active_project_id"] = self.project1_b.id
        session.save()
        
        response = self.client.get(reverse("upload-history"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "upload_1a.csv")
        
    def test_idor_cross_tenant_access_denied(self):
        """Verify that users cannot access views or data belonging to another tenant/project they don't belong to."""
        # Log in as External User (belongs to Org 2, has access to project2)
        self.client.login(username="external_user", password="password")
        
        # Try to view details of billing upload from Project 1A (upload_1a)
        detail_url = reverse("upload-detail", kwargs={"pk": self.upload_1a.id})
        response = self.client.get(detail_url)
        # Should return 404/403 or redirect because it belongs to project1_a
        self.assertIn(response.status_code, [403, 404])
        
        # Try to switch active project to project1_a (unauthorized)
        switch_url = reverse("accounts:switch-project")
        response = self.client.post(switch_url, {"project_id": self.project1_a.id})
        # Should redirect to projects list and show warning
        self.assertRedirects(response, reverse("accounts:project-list"))

    def test_rbac_capability_boundaries(self):
        """Verify capability restrictions for different roles: VIEWER, ANALYST, ADMIN, OWNER."""
        # 1. VIEWER cannot upload files
        self.client.login(username="viewer_user", password="password")
        session = self.client.session
        session["active_project_id"] = self.project1_a.id
        session.save()
        
        # Accessing billing upload page
        response = self.client.get(reverse("upload-billing"))
        # Should fail with 403 because VIEWER doesn't have UPLOAD_BILLING permission
        self.assertEqual(response.status_code, 403)
        
        # 2. ANALYST can run cost simulation and view members, but cannot manage roles/add members
        self.client.login(username="analyst_user", password="password")
        session = self.client.session
        session["active_project_id"] = self.project1_a.id
        session.save()
        
        simulator_url = reverse("cost-simulator")
        response = self.client.get(simulator_url)
        self.assertEqual(response.status_code, 200)
        
        # Analyst tries to view member page (should be 200 list view, but can_manage is False)
        response = self.client.get(reverse("accounts:org-members"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_manage"])
        
        # Analyst tries to POST role update (should return 403)
        # Create a dummy membership first
        from accounts.models import OrganizationMembership
        membership = OrganizationMembership.objects.filter(user=self.viewer_user, organization=self.org1).first()
        update_url = reverse("accounts:update-member-role", kwargs={"membership_id": membership.id})
        response = self.client.post(update_url, {"role": "ADMIN"})
        self.assertEqual(response.status_code, 403)

        # 3. ADMIN/OWNER can manage members
        self.client.login(username="admin_user", password="password")
        session = self.client.session
        session["active_project_id"] = self.project1_a.id
        session.save()
        response = self.client.get(reverse("accounts:org-members"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_manage"])

    def test_project_switching_behavior(self):
        """Verify switching active projects updates session, and invalid project selection redirects."""
        self.client.login(username="owner_user", password="password")
        
        # Switch to project1_b
        response = self.client.post(reverse("accounts:switch-project"), {"project_id": self.project1_b.id})
        self.assertRedirects(response, reverse("dashboard-home"))
        self.assertEqual(self.client.session["active_project_id"], self.project1_b.id)
        
        # Switch to an invalid project (project2, which belongs to Org 2 and owner_user does not belong to)
        response = self.client.post(reverse("accounts:switch-project"), {"project_id": self.project2.id})
        self.assertRedirects(response, reverse("accounts:project-list"))
        # Active project ID should NOT be project2
        self.assertNotEqual(self.client.session.get("active_project_id"), self.project2.id)

    def test_member_management_and_roles(self):
        """Verify member invitation (existing accounts), role updates, and member removal."""
        self.client.login(username="owner_user", password="password")
        session = self.client.session
        session["active_project_id"] = self.project1_a.id
        session.save()
        
        # 1. Invite an existing user by email
        invite_email = "external@example.com"  # external_user
        response = self.client.post(reverse("accounts:org-members"), {
            "email": invite_email,
            "role": "ANALYST"
        })
        self.assertRedirects(response, reverse("accounts:org-members"))
        
        # Check membership exists
        from accounts.models import OrganizationMembership
        membership = OrganizationMembership.objects.get(user=self.external_user, organization=self.org1)
        self.assertEqual(membership.role, "ANALYST")
        
        # 2. Update member role
        update_url = reverse("accounts:update-member-role", kwargs={"membership_id": membership.id})
        response = self.client.post(update_url, {"role": "ADMIN"})
        self.assertRedirects(response, reverse("accounts:org-members"))
        membership.refresh_from_db()
        self.assertEqual(membership.role, "ADMIN")
        
        # 3. Try to invite a non-existing user by email
        response = self.client.post(reverse("accounts:org-members"), {
            "email": "notreal@example.com",
            "role": "VIEWER"
        }, follow=True)
        self.assertContains(response, "No registered account found with that email address.")
        
        # 4. Remove member
        remove_url = reverse("accounts:remove-member", kwargs={"membership_id": membership.id})
        response = self.client.post(remove_url)
        self.assertRedirects(response, reverse("accounts:org-members"))
        # Check membership is deleted
        self.assertFalse(OrganizationMembership.objects.filter(id=membership.id).exists())


