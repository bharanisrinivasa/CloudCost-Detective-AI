from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .services import get_dashboard_metrics


@login_required
def dashboard_home(request):
    """Render the interactive dashboard home page for authenticated users."""
    filters = {
        'start_date': request.GET.get('start_date'),
        'end_date': request.GET.get('end_date'),
        'service': request.GET.get('service'),
        'region': request.GET.get('region'),
    }
    
    context = get_dashboard_metrics(request.user, filters)
    
    # Import and aggregate Module 5 anomaly KPI counters scoped to request.user
    from analytics.models import CostAnomaly
    context['open_anomalies_count'] = CostAnomaly.objects.filter(user=request.user, status="OPEN").count()
    context['critical_anomalies_count'] = CostAnomaly.objects.filter(user=request.user, status="OPEN", severity="CRITICAL").count()
    
    return render(request, "dashboard/home.html", context)


