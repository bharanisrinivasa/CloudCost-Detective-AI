"""
URL configuration for cloud_cost_detective project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from django.conf import settings
from django.conf.urls.static import static

from accounts.views import CustomLoginView, register_view, logout_view, profile_view
from cloud_cost_detective.views import health_check, readiness_check

urlpatterns = [
    path("", RedirectView.as_view(url="/dashboard/", permanent=True)),
    path("admin/", admin.site.urls),
    
    path("health/", health_check, name="health-check"),
    path("ready/", readiness_check, name="readiness-check"),
    
    # Global/legacy URL paths to support legacy non-namespaced reverses in tests
    path("accounts/login/", CustomLoginView.as_view(), name="login"),
    path("accounts/register/", register_view, name="register"),
    path("accounts/logout/", logout_view, name="logout"),
    path("accounts/profile/", profile_view, name="profile"),
    
    path("accounts/", include("accounts.urls")),
    path("billing/", include("billing.urls")),
    path("dashboard/", include("dashboard.urls")),
    path("analytics/", include("analytics.urls")),
    path("ai-engine/", include("ai_engine.urls")),
    path("ai/", include("ai_engine.urls")),
    path("api/", include("api.urls")),
    path("oci/", include("oci_connector.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

