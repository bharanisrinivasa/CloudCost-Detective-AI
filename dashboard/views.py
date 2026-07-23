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
    
    # Import and aggregate Module 6 waste KPI counters scoped to request.user
    from analytics.models import WasteFinding
    from django.db.models import Sum
    from decimal import Decimal
    
    open_waste = WasteFinding.objects.filter(user=request.user, status="OPEN")
    context['open_waste_count'] = open_waste.count()
    
    savings_by_currency = (
        open_waste.values('currency')
        .annotate(total_savings=Sum('estimated_monthly_savings'))
        .order_by('currency')
    )
    savings_parts = []
    for item in savings_by_currency:
        savings_parts.append(f"{item['total_savings']:.2f} {item['currency']}")
    
    context['potential_waste_savings'] = ", ".join(savings_parts) if savings_parts else "0.00 USD"
    context['waste_has_multiple_currencies'] = len(savings_by_currency) > 1
    
    return render(request, "dashboard/home.html", context)


