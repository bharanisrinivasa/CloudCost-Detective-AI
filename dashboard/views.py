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
    
    # Import and aggregate Module 9 recommendation KPI counters scoped to request.user
    from ai_engine.models import Recommendation
    open_recs = Recommendation.objects.filter(user=request.user, status="OPEN")
    context['open_recommendations_count'] = open_recs.count()
    context['high_priority_recommendations_count'] = open_recs.filter(priority__in=["HIGH", "CRITICAL"]).count()
    
    rec_savings = {}
    seen_waste_ids = set()
    for rec in open_recs:
        if rec.estimated_monthly_savings is None:
            continue
        curr = rec.currency or "USD"
        if rec.savings_source == "WASTE_FINDING" and rec.source_id is not None:
            if rec.source_id in seen_waste_ids:
                continue
            seen_waste_ids.add(rec.source_id)
        if curr not in rec_savings:
            rec_savings[curr] = Decimal("0.00")
        rec_savings[curr] += rec.estimated_monthly_savings

    rec_savings_parts = [f"{val:.2f} {cur}" for cur, val in rec_savings.items()]
    context['potential_recommendation_savings'] = ", ".join(rec_savings_parts) if rec_savings_parts else "0.00 USD"
    
    # Module 10 forecasting integration
    from analytics.services.cost_forecaster import get_forecast_for_user
    forecast_results = get_forecast_for_user(request.user)
    
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
    
    return render(request, "dashboard/home.html", context)


