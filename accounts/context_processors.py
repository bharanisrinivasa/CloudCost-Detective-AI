from accounts.permissions import get_active_project
from accounts.models import Project, OrganizationMembership

def active_project_processor(request):
    if not request.user or not request.user.is_authenticated:
        return {}
        
    active_project = get_active_project(request)
    
    # User's projects
    memberships = OrganizationMembership.objects.filter(user=request.user).select_related("organization")
    orgs = [m.organization for m in memberships]
    user_projects = Project.objects.filter(organization__in=orgs).select_related("organization")
    
    return {
        "active_project": active_project,
        "user_projects": user_projects,
    }
