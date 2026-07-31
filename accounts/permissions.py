from django.shortcuts import get_object_or_404, redirect
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.urls import reverse
from accounts.models import Project, OrganizationMembership

# Capability mapping
CAPABILITIES = {
    "VIEW_DASHBOARD": ["OWNER", "ADMIN", "ANALYST", "VIEWER"],
    "VIEW_BILLING": ["OWNER", "ADMIN", "ANALYST", "VIEWER"],
    "UPLOAD_BILLING": ["OWNER", "ADMIN", "ANALYST"],
    "DELETE_BILLING": ["OWNER", "ADMIN", "ANALYST"],
    "RUN_ANALYTICS": ["OWNER", "ADMIN", "ANALYST"],
    "UPDATE_ANALYTICS_STATUS": ["OWNER", "ADMIN", "ANALYST"],
    "VIEW_REPORTS": ["OWNER", "ADMIN", "ANALYST", "VIEWER"],
    "GENERATE_REPORT": ["OWNER", "ADMIN", "ANALYST", "VIEWER"],
    "USE_SIMULATOR": ["OWNER", "ADMIN", "ANALYST", "VIEWER"],
    "USE_CHAT": ["OWNER", "ADMIN", "ANALYST", "VIEWER"],
    "GENERATE_AI": ["OWNER", "ADMIN", "ANALYST", "VIEWER"],
    "MANAGE_PROJECT": ["OWNER", "ADMIN"],
    "MANAGE_MEMBERS": ["OWNER", "ADMIN"],
    "MANAGE_ORGANIZATION": ["OWNER"],
    "MANAGE_OCI_CONNECTION": ["OWNER", "ADMIN"],
    "RUN_OCI_SYNC": ["OWNER", "ADMIN", "ANALYST"],
    "VIEW_OCI_CONNECTION": ["OWNER", "ADMIN", "ANALYST", "VIEWER"],
}

def has_project_permission(user, project, capability):
    """
    Check if a user has a specific capability for a given project.
    Determined by organization membership role.
    """
    if not user.is_authenticated or not project:
        return False
        
    membership = OrganizationMembership.objects.filter(
        user=user,
        organization=project.organization
    ).first()
    
    if not membership:
        return False
        
    allowed_roles = CAPABILITIES.get(capability, [])
    return membership.role in allowed_roles


def get_active_project(request):
    """
    Retrieve the active project for the request session after validation.
    If session contains a stale/inaccessible project, clears the active_project_id
    and requires explicit valid project selection (returns None).
    If no project is in the session (e.g. first login), attempts to fall back to the first available project.
    """
    if not request.user.is_authenticated:
        return None

    session = getattr(request, "session", None)
    active_project_id = session.get("active_project_id") if session else None
    project = None

    if active_project_id:
        try:
            # Revalidate project existence and membership
            project = Project.objects.select_related("organization").get(pk=active_project_id)
            membership_exists = OrganizationMembership.objects.filter(
                user=request.user,
                organization=project.organization
            ).exists()
            if not membership_exists:
                if session and "active_project_id" in session:
                    del session["active_project_id"]
                return None
        except Project.DoesNotExist:
            if session and "active_project_id" in session:
                del session["active_project_id"]
            return None
    else:
        # Fallback if no project in session at all (first-login / default-project initialization flow)
        org_ids = OrganizationMembership.objects.filter(user=request.user).values_list("organization_id", flat=True)
        project = Project.objects.filter(organization_id__in=org_ids).first()
        if project and session:
            session["active_project_id"] = project.id

    return project


class ActiveProjectRequiredMixin:
    """Mixin for Class-Based Views that ensures an active project exists and injects it into self.active_project."""
    def dispatch(self, request, *args, **kwargs):
        self.active_project = get_active_project(request)
        if not self.active_project:
            messages.warning(request, "You must select or create a project first.")
            # Redirect to project management or home
            return redirect("accounts:project-list")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs) if hasattr(super(), "get_context_data") else {}
        context["active_project"] = self.active_project
        return context


class ProjectPermissionRequiredMixin(ActiveProjectRequiredMixin):
    """Mixin that checks capability permissions on the active project."""
    required_capability = None

    def dispatch(self, request, *args, **kwargs):
        # First ensure active project is resolved
        dispatch_response = super().dispatch(request, *args, **kwargs)
        if not self.active_project:
            return dispatch_response
            
        if not self.required_capability:
            raise ValueError("View subclass must define required_capability.")
            
        if not has_project_permission(request.user, self.active_project, self.required_capability):
            raise PermissionDenied("You do not have permission to perform this action.")
            
        return dispatch_response
