from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


class CustomUserAdmin(UserAdmin):
    """Custom User admin panel display and fields configuration."""
    model = User
    list_display = ("username", "email", "organization", "date_joined", "is_staff")
    list_filter = ("is_staff", "is_superuser", "is_active", "groups")
    search_fields = ("username", "email", "organization")
    ordering = ("username",)
    
    # Custom fieldsets to display our new fields in detail forms
    fieldsets = UserAdmin.fieldsets + (
        ("Custom Profiles Details", {"fields": ("organization", "phone_number", "profile_picture")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Custom Profiles Details", {"fields": ("organization", "phone_number", "profile_picture")}),
    )


admin.site.register(User, CustomUserAdmin)

