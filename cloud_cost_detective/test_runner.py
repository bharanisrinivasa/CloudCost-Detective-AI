from django.test.runner import DiscoverRunner
from django.db.models.signals import pre_save

def auto_assign_project_for_tests(sender, instance, **kwargs):
    # Check if the model has a project field and it is not set
    if hasattr(instance, "project") and getattr(instance, "project_id", None) is None:
        # Determine associated user
        user = getattr(instance, "user", None) or getattr(instance, "uploaded_by", None)
        if not user:
            from django.contrib.auth import get_user_model
            user = get_user_model().objects.first()
            if not user:
                user = get_user_model().objects.create_user(username="test_auto_project_user", password="pwd")
        
        # Resolve or create a default project for the user
        from accounts.models import Project, Organization, OrganizationMembership
        project = Project.objects.filter(organization__memberships__user=user).first()
        if not project:
            # Create a default org
            from django.utils.text import slugify
            org_name = f"Auto Org for {user.username}"
            base_slug = slugify(org_name) or "auto-org"
            org_slug = base_slug
            counter = 1
            while Organization.objects.filter(slug=org_slug).exists():
                org_slug = f"{base_slug}-{counter}"
                counter += 1
                
            org = Organization.objects.create(name=org_name, slug=org_slug)
            OrganizationMembership.objects.create(user=user, organization=org, role="OWNER")
            
            # Create a default project
            proj_name = f"Auto Project for {user.username}"
            base_p_slug = slugify(proj_name) or "auto-project"
            proj_slug = base_p_slug
            counter = 1
            while Project.objects.filter(organization=org, slug=proj_slug).exists():
                proj_slug = f"{base_p_slug}-{counter}"
                counter += 1
            project = Project.objects.create(name=proj_name, organization=org, slug=proj_slug)
            
        instance.project = project

class CustomDiscoverRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        
        # Connect pre_save signals for all models with a project ForeignKey
        from billing.models import BillingUpload
        from analytics.models import CostAnomaly, WasteFinding
        from ai_engine.models import Recommendation, AIExplanation, ChatSession
        
        for model in [BillingUpload, CostAnomaly, WasteFinding, Recommendation, AIExplanation, ChatSession]:
            pre_save.connect(auto_assign_project_for_tests, sender=model, dispatch_uid=f"auto_project_{model.__name__}")
