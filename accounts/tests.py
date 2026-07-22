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

