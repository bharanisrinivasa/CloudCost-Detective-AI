from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from .forms import CustomUserCreationForm, UserProfileForm, CustomAuthenticationForm


class CustomLoginView(LoginView):
    """Custom LoginView that utilizes the Bootstrap-styled authentication form."""
    template_name = "accounts/login.html"
    authentication_form = CustomAuthenticationForm

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Welcome back, {self.request.user.username}!")
        return response


def register_view(request):
    """View to handle user registration."""
    if request.user.is_authenticated:
        return redirect("dashboard-home")

    if request.method == "POST":
        form = CustomUserCreationForm(request.POST, request.FILES)
        if form.is_valid():
            user = form.save()
            # Automatically log the user in after registration
            login(request, user)
            messages.success(request, "Registration successful! Welcome to CloudCost Detective AI.")
            return redirect("dashboard-home")
        else:
            messages.error(request, "Registration failed. Please correct the errors below.")
    else:
        form = CustomUserCreationForm()

    return render(request, "accounts/register.html", {"form": form})


@login_required
def logout_view(request):
    """View to handle logging out the user."""
    logout(request)
    messages.info(request, "You have been logged out successfully.")
    return redirect("accounts:login")


@login_required
def profile_view(request):
    """View to view and update user profile information."""
    if request.method == "POST":
        form = UserProfileForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Your profile has been updated successfully.")
            return redirect("accounts:profile")
        else:
            messages.error(request, "Profile update failed. Please correct the errors below.")
    else:
        form = UserProfileForm(instance=request.user)

    return render(request, "accounts/profile.html", {"form": form})


# --- MODULE 13: MULTI-USER & TEAM VIEWS ---
from django.core.exceptions import PermissionDenied
from django.views.decorators.http import require_POST
from accounts.models import Organization, OrganizationMembership, Project
from accounts.permissions import get_active_project, has_project_permission

@login_required
def project_list_view(request):
    """Lists all organizations and projects the user has access to, and handles creation."""
    memberships = OrganizationMembership.objects.filter(user=request.user).select_related("organization")
    organizations = [m.organization for m in memberships]
    
    # Projects in these organizations
    projects = Project.objects.filter(organization__in=organizations).select_related("organization")
    
    active_project = get_active_project(request)
    
    # Form processing
    if request.method == "POST":
        action = request.POST.get("action")
        
        if action == "create_organization":
            name = request.POST.get("org_name", "").strip()
            if name:
                from django.utils.text import slugify
                base_slug = slugify(name) or "org"
                org_slug = base_slug
                counter = 1
                while Organization.objects.filter(slug=org_slug).exists():
                    org_slug = f"{base_slug}-{counter}"
                    counter += 1
                
                org = Organization.objects.create(name=name, slug=org_slug)
                # Creator becomes OWNER
                OrganizationMembership.objects.create(
                    user=request.user,
                    organization=org,
                    role="OWNER"
                )
                messages.success(request, f"Organization '{name}' created successfully.")
                return redirect("accounts:project-list")
            else:
                messages.error(request, "Organization name cannot be empty.")
                
        elif action == "create_project":
            name = request.POST.get("project_name", "").strip()
            org_id = request.POST.get("organization_id")
            
            if name and org_id:
                try:
                    org = Organization.objects.get(pk=org_id)
                except Organization.DoesNotExist:
                    messages.error(request, "Selected organization does not exist.")
                    return redirect("accounts:project-list")
                
                # Check user is OWNER or ADMIN of the org
                user_membership = OrganizationMembership.objects.filter(
                    user=request.user,
                    organization=org
                ).first()
                
                if not user_membership or user_membership.role not in ["OWNER", "ADMIN"]:
                    messages.error(request, "You do not have permission to create projects in this organization.")
                    return redirect("accounts:project-list")
                
                from django.utils.text import slugify
                base_slug = slugify(name) or "project"
                project_slug = base_slug
                counter = 1
                while Project.objects.filter(organization=org, slug=project_slug).exists():
                    project_slug = f"{base_slug}-{counter}"
                    counter += 1
                    
                project = Project.objects.create(name=name, organization=org, slug=project_slug)
                
                # Set as active project in session
                request.session["active_project_id"] = project.id
                messages.success(request, f"Project '{name}' created successfully.")
                return redirect("dashboard-home")
            else:
                messages.error(request, "Project name and organization are required.")
                
    return render(request, "accounts/projects.html", {
        "memberships": memberships,
        "organizations": organizations,
        "projects": projects,
        "active_project": active_project,
    })


@login_required
@require_POST
def switch_project_view(request):
    """POST-only project switching endpoint with validation."""
    project_id = request.POST.get("project_id")
    if not project_id:
        messages.error(request, "No project specified.")
        return redirect("accounts:project-list")
        
    try:
        project = Project.objects.select_related("organization").get(pk=project_id)
    except Project.DoesNotExist:
        messages.error(request, "Selected project does not exist.")
        return redirect("accounts:project-list")
        
    # Revalidate access
    membership_exists = OrganizationMembership.objects.filter(
        user=request.user,
        organization=project.organization
    ).exists()
    
    if not membership_exists:
        messages.error(request, "You do not have permission to access this project.")
        return redirect("accounts:project-list")
        
    request.session["active_project_id"] = project.id
    messages.success(request, f"Switched to project '{project.name}'.")
    
    # Redirect to referring page or dashboard
    referer = request.META.get("HTTP_REFERER")
    if referer and "accounts/projects/" not in referer:
        return redirect(referer)
    return redirect("dashboard-home")


@login_required
def org_members_view(request):
    """Lists organization members and handles invitation/role updates."""
    active_project = get_active_project(request)
    if not active_project:
        messages.warning(request, "Please select or create a project first.")
        return redirect("accounts:project-list")
        
    organization = active_project.organization
    
    # Verify current user membership
    my_membership = OrganizationMembership.objects.filter(
        user=request.user,
        organization=organization
    ).first()
    
    if not my_membership:
        raise PermissionDenied("You do not belong to this organization.")
        
    # Check permission
    can_manage = has_project_permission(request.user, active_project, "MANAGE_MEMBERS")
    
    # Process POST actions (invite/add member)
    if request.method == "POST" and can_manage:
        email = request.POST.get("email", "").strip()
        role = request.POST.get("role", "VIEWER").upper()
        
        # Server-side validation of roles
        if role not in ["OWNER", "ADMIN", "ANALYST", "VIEWER"]:
            raise PermissionDenied("Invalid role selection.")
            
        if role == "OWNER" and my_membership.role != "OWNER":
            raise PermissionDenied("Only Owners can designate other Owners.")
            
        if my_membership.role == "ADMIN" and role == "OWNER":
            raise PermissionDenied("Admins cannot assign a role above Admin.")
        
        if email:
            from django.contrib.auth import get_user_model
            UserModel = get_user_model()
            try:
                invited_user = UserModel.objects.get(email=email)
            except UserModel.DoesNotExist:
                messages.error(request, "No registered account found with that email address.")
                return redirect("accounts:org-members")
                
            # Check if user is already a member
            existing = OrganizationMembership.objects.filter(
                user=invited_user,
                organization=organization
            ).exists()
            
            if existing:
                messages.warning(request, f"User '{invited_user.username}' is already a member of this organization.")
            else:
                OrganizationMembership.objects.create(
                    user=invited_user,
                    organization=organization,
                    role=role
                )
                messages.success(request, f"User '{invited_user.username}' added to organization as {role}.")
            return redirect("accounts:org-members")
            
    # List members
    members = OrganizationMembership.objects.filter(organization=organization).select_related("user")
    
    return render(request, "accounts/org_members.html", {
        "active_project": active_project,
        "organization": organization,
        "members": members,
        "my_membership": my_membership,
        "can_manage": can_manage,
        "roles": ["OWNER", "ADMIN", "ANALYST", "VIEWER"],
    })


@login_required
@require_POST
def update_member_role_view(request, membership_id):
    """Updates a member's role within the organization."""
    active_project = get_active_project(request)
    if not active_project:
        return redirect("accounts:project-list")
        
    if not has_project_permission(request.user, active_project, "MANAGE_MEMBERS"):
        raise PermissionDenied("You do not have permission to manage members.")
        
    membership = get_object_or_404(OrganizationMembership, pk=membership_id, organization=active_project.organization)
    
    # Prevent editing own membership or demoting/promoting to OWNER if not owner
    my_membership = OrganizationMembership.objects.filter(
        user=request.user,
        organization=active_project.organization
    ).first()
    
    if not my_membership:
        raise PermissionDenied("You do not belong to this organization.")
        
    if membership.user == request.user:
        new_role = request.POST.get("role", "").upper()
        if membership.role == "OWNER" and new_role != "OWNER":
            from django.db import transaction
            with transaction.atomic():
                owners_count = OrganizationMembership.objects.select_for_update().filter(
                    organization=active_project.organization,
                    role="OWNER"
                ).count()
                if owners_count <= 1:
                    raise PermissionDenied("Cannot demote the final Owner. The organization must have at least one Owner.")
        raise PermissionDenied("You cannot modify your own role.")
        
    if membership.role == "OWNER" and my_membership.role != "OWNER":
        raise PermissionDenied("Only Owners can modify other Owners' roles.")
        
    new_role = request.POST.get("role", "").upper()
    if new_role not in ["OWNER", "ADMIN", "ANALYST", "VIEWER"]:
        raise PermissionDenied("Invalid role selection.")
        
    if new_role == "OWNER" and my_membership.role != "OWNER":
        raise PermissionDenied("Only Owners can designate other Owners.")
        
    if my_membership.role == "ADMIN" and new_role == "OWNER":
        raise PermissionDenied("Admins cannot assign a role above Admin.")
        
    # Final OWNER protection from demotion
    if membership.role == "OWNER" and new_role != "OWNER":
        from django.db import transaction
        with transaction.atomic():
            owners_count = OrganizationMembership.objects.select_for_update().filter(
                organization=active_project.organization,
                role="OWNER"
            ).count()
            if owners_count <= 1:
                raise PermissionDenied("Cannot demote the final Owner. The organization must have at least one Owner.")

    membership.role = new_role
    membership.save()
    messages.success(request, f"Updated role for '{membership.user.username}' to {new_role}.")
    return redirect("accounts:org-members")


@login_required
@require_POST
def remove_member_view(request, membership_id):
    """Removes a member from the organization."""
    active_project = get_active_project(request)
    if not active_project:
        return redirect("accounts:project-list")
        
    if not has_project_permission(request.user, active_project, "MANAGE_MEMBERS"):
        raise PermissionDenied("You do not have permission to manage members.")
        
    membership = get_object_or_404(OrganizationMembership, pk=membership_id, organization=active_project.organization)
    
    my_membership = OrganizationMembership.objects.filter(
        user=request.user,
        organization=active_project.organization
    ).first()
    
    if not my_membership:
        raise PermissionDenied("You do not belong to this organization.")
        
    if membership.role == "OWNER" and my_membership.role != "OWNER":
        raise PermissionDenied("Only Owners can remove other Owners.")
        
    # Self-removal (leaving the organization)
    if membership.user == request.user:
        # Protect final OWNER from leaving
        if membership.role == "OWNER":
            from django.db import transaction
            with transaction.atomic():
                owners_count = OrganizationMembership.objects.select_for_update().filter(
                    organization=active_project.organization,
                    role="OWNER"
                ).count()
                if owners_count <= 1:
                    raise PermissionDenied("Cannot leave the organization as the final Owner. You must designate another Owner first.")
        membership.delete()
        messages.success(request, "You have left the organization.")
        return redirect("accounts:project-list")
        
    # Removing someone else: protect final OWNER from removal
    if membership.role == "OWNER":
        from django.db import transaction
        with transaction.atomic():
            owners_count = OrganizationMembership.objects.select_for_update().filter(
                organization=active_project.organization,
                role="OWNER"
            ).count()
            if owners_count <= 1:
                raise PermissionDenied("Cannot remove the final Owner. The organization must have at least one Owner.")
                
    username = membership.user.username
    membership.delete()
    messages.success(request, f"Removed member '{username}' from the organization.")
    return redirect("accounts:org-members")

