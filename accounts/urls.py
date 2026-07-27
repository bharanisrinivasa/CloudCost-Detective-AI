from django.urls import path
from .views import (
    CustomLoginView, register_view, logout_view, profile_view,
    project_list_view, switch_project_view, org_members_view,
    update_member_role_view, remove_member_view
)

app_name = "accounts"

urlpatterns = [
    path("login/", CustomLoginView.as_view(), name="login"),
    path("register/", register_view, name="register"),
    path("logout/", logout_view, name="logout"),
    path("profile/", profile_view, name="profile"),
    
    # Project & Organization URLs
    path("projects/", project_list_view, name="project-list"),
    path("projects/switch/", switch_project_view, name="switch-project"),
    path("members/", org_members_view, name="org-members"),
    path("members/update/<int:membership_id>/", update_member_role_view, name="update-member-role"),
    path("members/remove/<int:membership_id>/", remove_member_view, name="remove-member"),
]
