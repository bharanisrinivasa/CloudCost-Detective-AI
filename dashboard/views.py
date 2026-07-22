from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from billing.models import BillingUpload


@login_required
def dashboard_home(request):
    """Render the dashboard home page for authenticated users, showing metadata."""
    total_uploads = BillingUpload.objects.count()
    return render(request, "dashboard/home.html", {
        "total_uploads": total_uploads
    })


