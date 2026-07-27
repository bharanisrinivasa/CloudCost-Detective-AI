import logging
from django.db import transaction, DatabaseError
from django.utils.text import slugify
from accounts.models import Organization, OrganizationMembership, Project

logger = logging.getLogger(__name__)

def provision_default_tenant(user):
    """
    Safely and deterministically provisions a default Organization, OWNER membership,
    and a default Project for the given user if they don't already have organization memberships.
    """
    # Avoid duplicate organization/project creation
    if OrganizationMembership.objects.filter(user=user).exists():
        return None, None, None

    try:
        with transaction.atomic():
            # Create a default organization for the user
            org_name = f"{user.username}'s Org"
            base_slug = slugify(org_name) or f"org-{user.pk}"
            org_slug = base_slug
            counter = 1
            while Organization.objects.filter(slug=org_slug).exists():
                org_slug = f"{base_slug}-{counter}"
                counter += 1
                
            org, org_created = Organization.objects.get_or_create(
                slug=org_slug,
                defaults={"name": org_name}
            )
            
            membership, mem_created = OrganizationMembership.objects.get_or_create(
                user=user,
                organization=org,
                defaults={"role": "OWNER"}
            )
            
            # Create a default project
            proj_name = "Default Project"
            base_proj_slug = slugify(proj_name) or "default-project"
            proj_slug = base_proj_slug
            counter = 1
            while Project.objects.filter(organization=org, slug=proj_slug).exists():
                proj_slug = f"{base_proj_slug}-{counter}"
                counter += 1
                
            project, proj_created = Project.objects.get_or_create(
                organization=org,
                slug=proj_slug,
                defaults={"name": proj_name}
            )
            
            return org, membership, project
    except DatabaseError as e:
        logger.warning("DatabaseError during default tenant provisioning: %s", e)
        return None, None, None
