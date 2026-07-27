import datetime
from decimal import Decimal
from collections import defaultdict
from django.utils import timezone
from django.db.models import Sum
from billing.models import BillingRecord, BillingUpload
from analytics.models import CostAnomaly, WasteFinding
from ai_engine.models import Recommendation
from analytics.services.cost_forecaster import get_forecast_for_user, get_forecast_for_project

def resolve_report_period(period, start_date_str=None, end_date_str=None):
    """
    Resolves the date range for the executive report based on project timezone.
    Returns (start_date, end_date) as datetime.date objects.
    """
    today = timezone.localdate()
    if period == "CURRENT_MONTH":
        start_date = today.replace(day=1)
        if today.month == 12:
            next_month = today.replace(year=today.year + 1, month=1, day=1)
        else:
            next_month = today.replace(month=today.month + 1, day=1)
        end_date = next_month - datetime.timedelta(days=1)
        return start_date, end_date
        
    elif period == "LAST_MONTH":
        first_of_this_month = today.replace(day=1)
        end_date = first_of_this_month - datetime.timedelta(days=1)
        start_date = end_date.replace(day=1)
        return start_date, end_date
        
    elif period == "LAST_30_DAYS":
        start_date = today - datetime.timedelta(days=29)
        end_date = today
        return start_date, end_date
        
    elif period == "CUSTOM":
        if not start_date_str or not end_date_str:
            raise ValueError("Both start_date and end_date are required for CUSTOM period.")
        try:
            start_date = datetime.datetime.strptime(start_date_str.strip(), "%Y-%m-%d").date()
            end_date = datetime.datetime.strptime(end_date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Dates must be in YYYY-MM-DD format.")
            
        if start_date > end_date:
            raise ValueError("Start date cannot be after end date.")
        return start_date, end_date
    else:
        raise ValueError(f"Invalid period selection: {period}")

def get_deduplicated_savings(recommendations, default_currency="UNKNOWN"):
    """
    Calculate deduplicated potential savings grouped by currency.
    If recommendation savings are inherited from the same WasteFinding, count it once.
    """
    rec_savings = {}
    seen_waste_ids = set()
    for rec in recommendations:
        if rec.estimated_monthly_savings is None:
            continue
        curr = (rec.currency or "").strip() or default_currency
        if rec.savings_source == "WASTE_FINDING" and rec.source_id is not None:
            if rec.source_id in seen_waste_ids:
                continue
            seen_waste_ids.add(rec.source_id)
        if curr not in rec_savings:
            rec_savings[curr] = Decimal("0.00")
        rec_savings[curr] += rec.estimated_monthly_savings
    return rec_savings

def collect_report_data(user, period, start_date_str=None, end_date_str=None, enabled_sections=None):
    """
    DEPRECATED: Backward compatibility wrapper for user-based report collection.
    Use collect_report_data_for_project instead.
    """
    import warnings
    warnings.warn(
        "collect_report_data is deprecated. Use collect_report_data_for_project instead.",
        DeprecationWarning,
        stacklevel=2
    )
    from accounts.models import Project
    project = Project.objects.filter(organization__memberships__user=user).first()
    return collect_report_data_for_project(project, period, start_date_str, end_date_str, enabled_sections)

def collect_report_data_for_project(project, period, start_date_str=None, end_date_str=None, enabled_sections=None):
    """
    Queries and aggregates authoritative cost and optimization data for a specific project
    within the resolved report period.
    """
    if enabled_sections is None:
        enabled_sections = ["cost_breakdown", "anomalies", "waste", "recommendations", "forecast"]
        
    start_date, end_date = resolve_report_period(period, start_date_str, end_date_str)
    
    if not project:
        return {
            "start_date": start_date,
            "end_date": end_date,
            "generated_at": timezone.now(),
            "total_records_count": 0,
            "unique_resources_count": 0,
            "services_tracked_count": 0,
            "regions_tracked_count": 0,
            "open_anomalies_count": 0,
            "open_waste_count": 0,
            "open_recommendations_count": 0,
            "total_costs": {},
            "potential_savings": {},
            "services_breakdown": {},
            "regions_breakdown": {},
            "top_resources": {},
            "anomalies": [],
            "waste_findings": [],
            "recommendations": [],
            "forecast_results": {},
            "warnings": ["No active project selected."],
            "enabled_sections": enabled_sections
        }

    # 1. Base Querysets with strict project isolation
    records = BillingRecord.objects.filter(
        upload__project=project
    ).exclude(usage_start__isnull=True)
    
    # Filter BillingRecord by local datetime range
    in_period_records = []
    for r in records:
        usage_dt = r.usage_start
        if timezone.is_aware(usage_dt):
            usage_dt = timezone.localtime(usage_dt)
        usage_date = usage_dt.date()
        if start_date <= usage_date <= end_date:
            in_period_records.append(r)
            
    # Unique resources count in period (using non-empty resource_id)
    unique_resource_ids = set()
    for r in in_period_records:
        res_id = (r.resource_id or "").strip()
        if res_id:
            unique_resource_ids.add(res_id)
    unique_resources_count = len(unique_resource_ids)
    
    # Aggregate overall costs by currency
    total_costs_by_curr = defaultdict(Decimal)
    for r in in_period_records:
        curr = (r.currency or "").strip() or "UNKNOWN"
        total_costs_by_curr[curr] += r.cost
        
    # Service Breakdown
    service_costs = defaultdict(Decimal) # (service, currency) -> cost
    for r in in_period_records:
        svc = (r.service or "").strip() or "Unknown Service"
        curr = (r.currency or "").strip() or "UNKNOWN"
        service_costs[(svc, curr)] += r.cost
        
    # Region Breakdown
    region_costs = defaultdict(Decimal) # (region, currency) -> cost
    for r in in_period_records:
        reg = (r.region or "").strip() or "Unknown Region"
        curr = (r.currency or "").strip() or "UNKNOWN"
        region_costs[(reg, curr)] += r.cost
        
    # Top Resources
    resource_costs = defaultdict(Decimal) # (key, svc, reg, curr) -> cost
    for r in in_period_records:
        curr = (r.currency or "").strip() or "UNKNOWN"
        svc = (r.service or "").strip() or "Unknown Service"
        reg = (r.region or "").strip() or "Unknown Region"
        
        # Identity rules
        res_id = (r.resource_id or "").strip()
        res_name = (r.resource_name or "").strip()
        if res_id:
            res_key = res_id
        elif res_name:
            res_key = res_name
        else:
            res_key = "Unknown Resource"
            
        resource_costs[(res_key, svc, reg, curr)] += r.cost
        
    # Build Service List per currency (limit 10 per currency, cost desc)
    services_by_curr = defaultdict(list)
    for (svc, curr), cost in service_costs.items():
        pct = Decimal("0.00")
        total = total_costs_by_curr[curr]
        if total > 0:
            pct = (cost / total * Decimal("100")).quantize(Decimal("0.01"))
        services_by_curr[curr].append({
            "service": svc,
            "cost": cost.quantize(Decimal("0.01")),
            "percentage": pct
        })
    for curr in services_by_curr:
        services_by_curr[curr] = sorted(services_by_curr[curr], key=lambda x: (-x["cost"], x["service"]))[:10]
        
    # Build Region List per currency (limit 10 per currency, cost desc)
    regions_by_curr = defaultdict(list)
    for (reg, curr), cost in region_costs.items():
        pct = Decimal("0.00")
        total = total_costs_by_curr[curr]
        if total > 0:
            pct = (cost / total * Decimal("100")).quantize(Decimal("0.01"))
        regions_by_curr[curr].append({
            "region": reg,
            "cost": cost.quantize(Decimal("0.01")),
            "percentage": pct
        })
    for curr in regions_by_curr:
        regions_by_curr[curr] = sorted(regions_by_curr[curr], key=lambda x: (-x["cost"], x["region"]))[:10]
        
    # Build Top Resources List per currency (limit 10, cost desc)
    resources_by_curr = defaultdict(list)
    for (res_key, svc, reg, curr), cost in resource_costs.items():
        resources_by_curr[curr].append({
            "resource": res_key,
            "service": svc,
            "region": reg,
            "cost": cost.quantize(Decimal("0.01"))
        })
    for curr in resources_by_curr:
        resources_by_curr[curr] = sorted(resources_by_curr[curr], key=lambda x: (-x["cost"], x["resource"]))[:10]
        
    # 2. Cost Anomalies: filter by detected_date
    anomalies = CostAnomaly.objects.filter(
        project=project,
        detected_date__range=(start_date, end_date)
    )
    # Severity Ordering mapping
    severity_map = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    sorted_anomalies = sorted(
        anomalies,
        key=lambda x: (severity_map.get(x.severity, 4), -x.detected_date.toordinal(), x.pk)
    )[:20]
    
    # 3. Waste Findings: overlap select period: first_seen <= end_date AND last_seen >= start_date
    waste_findings = WasteFinding.objects.filter(
        project=project,
        first_seen__lte=end_date,
        last_seen__gte=start_date
    )
    confidence_map = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    sorted_waste = sorted(
        waste_findings,
        key=lambda x: (confidence_map.get(x.confidence, 3), -x.estimated_monthly_savings, x.pk)
    )[:20]
    
    # 4. Recommendations: created within report period (detected_at__date in range)
    recommendations = Recommendation.objects.filter(
        project=project,
        detected_at__date__range=(start_date, end_date)
    )
    priority_map = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    sorted_recs = sorted(
        recommendations,
        key=lambda x: (
            priority_map.get(x.priority, 4),
            -(x.estimated_monthly_savings if x.estimated_monthly_savings is not None else Decimal("-99999999")),
            x.pk
        )
    )[:20]
    
    # Deduplicate savings from recommendations
    deduped_savings = get_deduplicated_savings(recommendations)
    
    # KPI metrics (Total OPEN anomalies, waste, and recommendations)
    open_anomalies_count = CostAnomaly.objects.filter(project=project, status="OPEN").count()
    open_waste_count = WasteFinding.objects.filter(project=project, status="OPEN").count()
    open_recommendations_count = Recommendation.objects.filter(project=project, status="OPEN").count()
    
    # 5. Forecast
    forecast_data = {}
    if "forecast" in enabled_sections:
        forecast_data = get_forecast_for_project(project)
        
    # Data Quality Flags
    warnings = []
    has_blank_currency = any(r.currency is None or not str(r.currency).strip() for r in in_period_records)
    has_blank_service = any(r.service is None or not str(r.service).strip() for r in in_period_records)
    has_blank_region = any(r.region is None or not str(r.region).strip() for r in in_period_records)
    
    if len(total_costs_by_curr) > 1:
        warnings.append("Multiple currencies detected. Monetary values are reported separately and are not combined.")
    if has_blank_currency:
        warnings.append("Billing records with missing or unknown currencies were detected (categorized as UNKNOWN).")
    if has_blank_service:
        warnings.append("Billing records with missing service names were detected (categorized as Unknown Service).")
    if has_blank_region:
        warnings.append("Billing records with missing regions were detected (categorized as Unknown Region).")
        
    today = timezone.localdate()
    if start_date <= today <= end_date:
        warnings.append("The reporting period includes the current calendar month, which contains incomplete month-to-date (MTD) billing records.")
        
    # Quantize total costs
    final_totals = {c: val.quantize(Decimal("0.01")) for c, val in total_costs_by_curr.items()}
    final_savings = {c: val.quantize(Decimal("0.01")) for c, val in deduped_savings.items()}
    
    return {
        "start_date": start_date,
        "end_date": end_date,
        "generated_at": timezone.now(),
        "total_records_count": len(in_period_records),
        "unique_resources_count": unique_resources_count,
        "services_tracked_count": len(set(r.service for r in in_period_records if r.service)),
        "regions_tracked_count": len(set(r.region for r in in_period_records if r.region)),
        "open_anomalies_count": open_anomalies_count,
        "open_waste_count": open_waste_count,
        "open_recommendations_count": open_recommendations_count,
        "total_costs": final_totals,
        "potential_savings": final_savings,
        "services_breakdown": services_by_curr if "cost_breakdown" in enabled_sections else {},
        "regions_breakdown": regions_by_curr if "cost_breakdown" in enabled_sections else {},
        "top_resources": resources_by_curr if "cost_breakdown" in enabled_sections else {},
        "anomalies": sorted_anomalies if "anomalies" in enabled_sections else [],
        "waste_findings": sorted_waste if "waste" in enabled_sections else [],
        "recommendations": sorted_recs if "recommendations" in enabled_sections else [],
        "forecast_results": forecast_data,
        "warnings": warnings,
        "enabled_sections": enabled_sections
    }
