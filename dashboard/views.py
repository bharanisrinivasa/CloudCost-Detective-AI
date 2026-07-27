from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .services import get_dashboard_metrics


@login_required
def dashboard_home(request):
    """Render the interactive dashboard home page for authenticated users."""
    from accounts.permissions import get_active_project
    from django.shortcuts import redirect
    
    active_project = get_active_project(request)
    if not active_project:
        return redirect("accounts:project-list")

    filters = {
        'start_date': request.GET.get('start_date'),
        'end_date': request.GET.get('end_date'),
        'service': request.GET.get('service'),
        'region': request.GET.get('region'),
    }
    
    from dashboard.services.analytics import get_dashboard_metrics_for_project
    context = get_dashboard_metrics_for_project(active_project, filters)
    context['active_project'] = active_project
    
    # Import and aggregate Module 5 anomaly KPI counters scoped to project
    from analytics.models import CostAnomaly
    context['open_anomalies_count'] = CostAnomaly.objects.filter(project=active_project, status="OPEN").count()
    context['critical_anomalies_count'] = CostAnomaly.objects.filter(project=active_project, status="OPEN", severity="CRITICAL").count()
    
    # Import and aggregate Module 6 waste KPI counters scoped to project
    from analytics.models import WasteFinding
    from django.db.models import Sum
    from decimal import Decimal
    
    open_waste = WasteFinding.objects.filter(project=active_project, status="OPEN")
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
    
    # Import and aggregate Module 9 recommendation KPI counters scoped to project
    from ai_engine.models import Recommendation
    open_recs = Recommendation.objects.filter(project=active_project, status="OPEN")
    context['open_recommendations_count'] = open_recs.count()
    context['high_priority_recommendations_count'] = open_recs.filter(priority__in=["HIGH", "CRITICAL"]).count()
    
    from analytics.services.report_service import get_deduplicated_savings
    rec_savings = get_deduplicated_savings(open_recs, default_currency="USD")

    rec_savings_parts = [f"{val:.2f} {cur}" for cur, val in rec_savings.items()]
    context['potential_recommendation_savings'] = ", ".join(rec_savings_parts) if rec_savings_parts else "0.00 USD"
    
    # Module 10 forecasting integration
    from analytics.services.cost_forecaster import get_forecast_for_project
    forecast_results = get_forecast_for_project(active_project)
    
    forecast_available = False
    forecast_summaries = []
    for curr, res in forecast_results.items():
        if res.get("forecast_available"):
            forecast_available = True
            forecast_summaries.append({
                "currency": curr,
                "next_month_forecast": res["next_month_forecast"],
                "confidence": res["confidence"]
            })
            
    context['forecast_available'] = forecast_available
    context['forecast_summaries'] = forecast_summaries
    context['has_simulator'] = True
    context['has_reports'] = True
    
    return render(request, "dashboard/home.html", context)


