import datetime
import calendar
import logging
from decimal import Decimal
from typing import Optional
from django.utils import timezone
from django.db.models import Sum
from django.db.models.functions import TruncMonth, TruncDay
from billing.models import BillingRecord
from analytics.models import CostAnomaly, WasteFinding
from ai_engine.services.chat.intent_schema import ChatQueryPlan, IntentEnum, TimeRangeTypeEnum

logger = logging.getLogger(__name__)

def serialize_decimal(val: Optional[Decimal]) -> str:
    """Helper to convert a Decimal to a quantized 2-decimal-place string."""
    if val is None:
        return "0.00"
    return str(val.quantize(Decimal("0.01")))

def resolve_time_range(time_range_type: str, custom_start: Optional[str] = None, custom_end: Optional[str] = None) -> tuple[Optional[datetime.date], Optional[datetime.date]]:
    """Resolves a standardized time range into start and end dates relative to project local timezone."""
    today = timezone.localdate()
    
    if time_range_type == "ALL_TIME":
        return None, None
    elif time_range_type == "TODAY":
        return today, today
    elif time_range_type == "YESTERDAY":
        yesterday = today - datetime.timedelta(days=1)
        return yesterday, yesterday
    elif time_range_type == "THIS_WEEK":
        start_of_week = today - datetime.timedelta(days=today.weekday())
        return start_of_week, today
    elif time_range_type == "LAST_WEEK":
        start_of_last_week = today - datetime.timedelta(days=today.weekday() + 7)
        end_of_last_week = start_of_last_week + datetime.timedelta(days=6)
        return start_of_last_week, end_of_last_week
    elif time_range_type == "THIS_MONTH":
        start_of_month = today.replace(day=1)
        return start_of_month, today
    elif time_range_type == "LAST_MONTH":
        first_day_this_month = today.replace(day=1)
        last_day_last_month = first_day_this_month - datetime.timedelta(days=1)
        first_day_last_month = last_day_last_month.replace(day=1)
        return first_day_last_month, last_day_last_month
    elif time_range_type == "LAST_30_DAYS":
        # Exactly 30 calendar dates inclusive (29 days offset)
        start_date = today - datetime.timedelta(days=29)
        return start_date, today
    elif time_range_type == "CUSTOM":
        if not custom_start or not custom_end:
            raise ValueError("Custom dates missing.")
        start = datetime.datetime.strptime(custom_start, "%Y-%m-%d").date()
        end = datetime.datetime.strptime(custom_end, "%Y-%m-%d").date()
        return start, end
        
    return None, None

def execute_query_plan(user, plan: ChatQueryPlan) -> dict:
    """
    DEPRECATED: Backward compatibility wrapper for user-based query execution.
    Use execute_query_plan_for_project instead.
    """
    import warnings
    warnings.warn(
        "execute_query_plan is deprecated. Use execute_query_plan_for_project instead.",
        DeprecationWarning,
        stacklevel=2
    )
    from accounts.models import Project
    project = Project.objects.filter(organization__memberships__user=user).first()
    return execute_query_plan_for_project(project, user, plan)

def execute_query_plan_for_project(project, user, plan: ChatQueryPlan) -> dict:
    """Executes the query plan deterministically using Django ORM scoped to a project and returns serializable data."""
    intent = plan.intent

    # Fetch boundaries
    start, end = resolve_time_range(
        plan.time_range.type,
        plan.time_range.start_date,
        plan.time_range.end_date
    )

    if not project:
        return {"results": []}

    # Base query for project data isolation
    records = BillingRecord.objects.filter(upload__project=project)

    if start:
        records = records.filter(usage_start__date__gte=start)
    if end:
        records = records.filter(usage_start__date__lte=end)

    # Apply general filters
    if plan.filters.service:
        records = records.filter(service=plan.filters.service)
    if plan.filters.region:
        records = records.filter(region=plan.filters.region)

    limit = plan.limit or 5

    # 1. TOTAL_COST
    if intent == IntentEnum.TOTAL_COST:
        agg = records.values('currency').annotate(total=Sum('cost')).order_by('currency')
        results = []
        for item in agg:
            results.append({
                "currency": item['currency'] or "UNKNOWN",
                "total_cost": serialize_decimal(item['total'])
            })
        return {"results": results}

    # 2. SERVICE_COST / TOP_SERVICES
    elif intent in (IntentEnum.SERVICE_COST, IntentEnum.TOP_SERVICES):
        agg = records.values('service', 'currency').annotate(total=Sum('cost')).order_by('-total')[:limit]
        results = []
        for item in agg:
            results.append({
                "service": item['service'] or "Unknown Service",
                "currency": item['currency'] or "UNKNOWN",
                "total_cost": serialize_decimal(item['total'])
            })
        return {"results": results}

    # 3. REGION_COST / TOP_REGIONS
    elif intent in (IntentEnum.REGION_COST, IntentEnum.TOP_REGIONS):
        agg = records.values('region', 'currency').annotate(total=Sum('cost')).order_by('-total')[:limit]
        results = []
        for item in agg:
            results.append({
                "region": item['region'] or "Unknown Region",
                "currency": item['currency'] or "UNKNOWN",
                "total_cost": serialize_decimal(item['total'])
            })
        return {"results": results}

    # 4. RESOURCE_COST / TOP_RESOURCES
    elif intent in (IntentEnum.RESOURCE_COST, IntentEnum.TOP_RESOURCES):
        # Additional resource identity grouping in python
        if plan.filters.resource_id:
            records = records.filter(resource_id=plan.filters.resource_id)
        if plan.filters.resource_name:
            records = records.filter(resource_name=plan.filters.resource_name)

        agg = records.values('resource_id', 'resource_name', 'service', 'region', 'currency').annotate(total=Sum('cost'))
        
        resource_map = {}
        for item in agg:
            r_id = (item['resource_id'] or '').strip()
            r_name = (item['resource_name'] or '').strip()
            curr = item['currency'] or "UNKNOWN"
            cost = item['total'] or Decimal("0.00")
            
            if r_id:
                key = f"id:{r_id}"
            elif r_name:
                key = f"name:{r_name}"
            else:
                key = "unknown"

            g_key = (key, curr)
            if g_key not in resource_map:
                resource_map[g_key] = {
                    "resource_key": key,
                    "resource_id": r_id,
                    "resource_name": r_name,
                    "service": item['service'] or "Unknown Service",
                    "region": item['region'] or "Unknown Region",
                    "currency": curr,
                    "total_cost": Decimal("0.00")
                }
            resource_map[g_key]["total_cost"] += cost

        results = list(resource_map.values())
        results.sort(key=lambda x: x['total_cost'], reverse=True)
        results = results[:limit]
        
        for r in results:
            r['total_cost'] = serialize_decimal(r['total_cost'])
        return {"results": results}

    # 5. COST_TREND
    elif intent == IntentEnum.COST_TREND:
        if start and end and (end - start).days <= 31:
            trend_agg = (
                records.filter(usage_start__isnull=False)
                .annotate(period=TruncDay('usage_start'))
                .values('period', 'currency')
                .annotate(total=Sum('cost'))
                .order_by('period')
            )
            format_str = "%Y-%m-%d"
        else:
            trend_agg = (
                records.filter(usage_start__isnull=False)
                .annotate(period=TruncMonth('usage_start'))
                .values('period', 'currency')
                .annotate(total=Sum('cost'))
                .order_by('period')
            )
            format_str = "%Y-%m"

        results = []
        for item in trend_agg:
            p_val = item['period']
            results.append({
                "period": p_val.strftime(format_str) if hasattr(p_val, 'strftime') else str(p_val),
                "currency": item['currency'] or "UNKNOWN",
                "total_cost": serialize_decimal(item['total'])
            })
        return {"results": results[:limit]}

    # 6. COST_COMPARISON
    elif intent == IntentEnum.COST_COMPARISON:
        if plan.comparison_services:
            records = records.filter(service__in=plan.comparison_services)
        agg = records.values('service', 'currency').annotate(total=Sum('cost')).order_by('-total')
        results = []
        for item in agg:
            results.append({
                "service": item['service'] or "Unknown Service",
                "currency": item['currency'] or "UNKNOWN",
                "total_cost": serialize_decimal(item['total'])
            })
        return {"results": results[:limit]}

    # 7. ANOMALIES
    elif intent == IntentEnum.ANOMALIES:
        qs = CostAnomaly.objects.filter(project=project)
        if start:
            qs = qs.filter(detected_date__gte=start)
        if end:
            qs = qs.filter(detected_date__lte=end)
        if plan.filters.anomaly_severity:
            qs = qs.filter(severity=plan.filters.anomaly_severity)
        if plan.filters.anomaly_status:
            qs = qs.filter(status=plan.filters.anomaly_status)

        qs = qs.order_by('-detected_date')[:limit]
        results = []
        for item in qs:
            results.append({
                "id": item.pk,
                "anomaly_type": item.anomaly_type,
                "detected_date": item.detected_date.isoformat(),
                "service_name": item.service_name or "Unknown Service",
                "resource_id": item.resource_id,
                "resource_name": item.resource_name,
                "region": item.region,
                "actual_cost": serialize_decimal(item.actual_cost),
                "expected_cost": serialize_decimal(item.expected_cost),
                "deviation_percentage": serialize_decimal(item.deviation_percentage),
                "severity": item.severity,
                "status": item.status
            })
        return {"results": results}

    # 8. WASTE_FINDINGS
    elif intent == IntentEnum.WASTE_FINDINGS:
        qs = WasteFinding.objects.filter(project=project)
        if plan.filters.waste_type:
            qs = qs.filter(waste_type=plan.filters.waste_type)
        if plan.filters.waste_confidence:
            qs = qs.filter(confidence=plan.filters.waste_confidence)
        if plan.filters.waste_status:
            qs = qs.filter(status=plan.filters.waste_status)

        qs = qs.order_by('-last_seen')[:limit]
        results = []
        for item in qs:
            results.append({
                "id": item.pk,
                "waste_type": item.waste_type,
                "resource_id": item.resource_id,
                "resource_name": item.resource_name,
                "service_name": item.service_name,
                "region": item.region,
                "currency": item.currency or "UNKNOWN",
                "first_seen": item.first_seen.isoformat(),
                "last_seen": item.last_seen.isoformat(),
                "total_cost": serialize_decimal(item.total_cost),
                "estimated_monthly_savings": serialize_decimal(item.estimated_monthly_savings),
                "confidence": item.confidence,
                "status": item.status
            })
        return {"results": results}

    # 9. POTENTIAL_SAVINGS
    elif intent == IntentEnum.POTENTIAL_SAVINGS:
        qs = WasteFinding.objects.filter(project=project)
        if plan.filters.waste_status:
            qs = qs.filter(status=plan.filters.waste_status)
        else:
            qs = qs.filter(status="OPEN")
        if plan.filters.waste_type:
            qs = qs.filter(waste_type=plan.filters.waste_type)
        if plan.filters.waste_confidence:
            qs = qs.filter(confidence=plan.filters.waste_confidence)

        agg = qs.values('currency').annotate(total_savings=Sum('estimated_monthly_savings')).order_by('currency')
        results = []
        for item in agg:
            results.append({
                "currency": item['currency'] or "UNKNOWN",
                "estimated_monthly_savings": serialize_decimal(item['total_savings'])
            })
        return {"results": results}

    # 10. HELP
    elif intent == IntentEnum.HELP:
        return {
            "results": {
                "supported_intents": [
                    {"name": "TOTAL_COST", "description": "Get total cost over a time range (supports service, region filters)."},
                    {"name": "SERVICE_COST", "description": "Get cost broken down by OCI service."},
                    {"name": "REGION_COST", "description": "Get cost broken down by region."},
                    {"name": "RESOURCE_COST", "description": "Get cost breakdown for specific OCI resources."},
                    {"name": "ANOMALIES", "description": "List cost anomaly occurrences (supports severity, status filters)."},
                    {"name": "WASTE_FINDINGS", "description": "List potential resource waste findings (supports confidence, status filters)."},
                    {"name": "POTENTIAL_SAVINGS", "description": "Show potential savings from shutting down or resizing idle resources."},
                    {"name": "COST_INCREASE_EXPLANATION", "description": "Explains why the bill changed between two calendar periods."}
                ],
                "example_questions": [
                    "Why did my bill increase?",
                    "Which service costs the most?",
                    "Show last month's cost.",
                    "Compare Compute and Storage costs.",
                    "How much potential monthly savings have we identified?",
                    "Show my critical anomalies."
                ]
            }
        }

    # 11. COST_INCREASE_EXPLANATION
    elif intent == IntentEnum.COST_INCREASE_EXPLANATION:
        today = timezone.localdate()
        if plan.time_range.type == TimeRangeTypeEnum.THIS_MONTH:
            # Current: 1st of current month to today
            start_curr = today.replace(day=1)
            end_curr = today
            
            # Previous: 1st of previous month to same elapsed day (capped at previous month's final day)
            if start_curr.month == 1:
                start_prev = start_curr.replace(year=start_curr.year - 1, month=12, day=1)
            else:
                start_prev = start_curr.replace(month=start_curr.month - 1, day=1)
                
            days_in_prev = calendar.monthrange(start_prev.year, start_prev.month)[1]
            elapsed_days = end_curr.day
            prev_day = min(elapsed_days, days_in_prev)
            end_prev = start_prev.replace(day=prev_day)
            
            comparison_is_equivalent_duration = (end_curr - start_curr).days == (end_prev - start_prev).days
        else:
            # Default: Last full calendar month vs immediately preceding full calendar month
            first_this = today.replace(day=1)
            last_prev = first_this - datetime.timedelta(days=1)
            start_curr = last_prev.replace(day=1)
            end_curr = last_prev
            
            last_prev_prev = start_curr - datetime.timedelta(days=1)
            start_prev = last_prev_prev.replace(day=1)
            end_prev = last_prev_prev
            
            comparison_is_equivalent_duration = (end_curr - start_curr).days == (end_prev - start_prev).days

        base_records = BillingRecord.objects.filter(upload__project=project)
        if plan.filters.service:
            base_records = base_records.filter(service=plan.filters.service)
        if plan.filters.region:
            base_records = base_records.filter(region=plan.filters.region)

        # Query totals
        curr_agg = base_records.filter(usage_start__date__gte=start_curr, usage_start__date__lte=end_curr).values('currency').annotate(total=Sum('cost'))
        prev_agg = base_records.filter(usage_start__date__gte=start_prev, usage_start__date__lte=end_prev).values('currency').annotate(total=Sum('cost'))
        
        curr_map = {item['currency'] or "UNKNOWN": item['total'] or Decimal("0.00") for item in curr_agg}
        prev_map = {item['currency'] or "UNKNOWN": item['total'] or Decimal("0.00") for item in prev_agg}
        
        all_currencies = set(curr_map.keys()).union(set(prev_map.keys()))
        if not all_currencies:
            all_currencies = {"UNKNOWN"}
            
        currency_comparisons = []
        contributors = {}

        for curr in all_currencies:
            curr_total = curr_map.get(curr, Decimal("0.00"))
            prev_total = prev_map.get(curr, Decimal("0.00"))
            change_abs = curr_total - prev_total
            
            # Safe Decimal percentage change calculations
            pct_change = None
            pct_change_reason = None
            if prev_total > 0:
                pct_change = serialize_decimal((change_abs / prev_total) * 100)
            elif prev_total == 0 and curr_total > 0:
                pct_change = None
                pct_change_reason = "NO_PREVIOUS_SPEND"
            elif prev_total == 0 and curr_total == 0:
                pct_change = "0.00"

            currency_comparisons.append({
                "currency": curr,
                "current_total": serialize_decimal(curr_total),
                "previous_total": serialize_decimal(prev_total),
                "change_absolute": serialize_decimal(change_abs),
                "percentage_change": pct_change,
                "percentage_change_reason": pct_change_reason
            })

            # Calculate top 3 positive contributors (spend increases) per currency
            curr_db_val = None if curr == "UNKNOWN" else curr
            
            # Services deltas
            c_svcs = base_records.filter(usage_start__date__gte=start_curr, usage_start__date__lte=end_curr, currency=curr_db_val).values('service').annotate(total=Sum('cost'))
            p_svcs = base_records.filter(usage_start__date__gte=start_prev, usage_start__date__lte=end_prev, currency=curr_db_val).values('service').annotate(total=Sum('cost'))
            
            c_svc_map = {item['service'] or "Unknown Service": item['total'] or Decimal("0.00") for item in c_svcs}
            p_svc_map = {item['service'] or "Unknown Service": item['total'] or Decimal("0.00") for item in p_svcs}
            
            all_svcs = set(c_svc_map.keys()).union(set(p_svc_map.keys()))
            svc_deltas = []
            for svc in all_svcs:
                delta = c_svc_map.get(svc, Decimal("0.00")) - p_svc_map.get(svc, Decimal("0.00"))
                if delta > 0:
                    svc_deltas.append({"service": svc, "delta": delta})
            svc_deltas.sort(key=lambda x: x['delta'], reverse=True)
            top_services = [{"service": x["service"], "delta": serialize_decimal(x["delta"])} for x in svc_deltas[:3]]

            # Region deltas
            c_regs = base_records.filter(usage_start__date__gte=start_curr, usage_start__date__lte=end_curr, currency=curr_db_val).values('region').annotate(total=Sum('cost'))
            p_regs = base_records.filter(usage_start__date__gte=start_prev, usage_start__date__lte=end_prev, currency=curr_db_val).values('region').annotate(total=Sum('cost'))
            
            c_reg_map = {item['region'] or "Unknown Region": item['total'] or Decimal("0.00") for item in c_regs}
            p_reg_map = {item['region'] or "Unknown Region": item['total'] or Decimal("0.00") for item in p_regs}
            
            all_regs = set(c_reg_map.keys()).union(set(p_reg_map.keys()))
            reg_deltas = []
            for reg in all_regs:
                delta = c_reg_map.get(reg, Decimal("0.00")) - p_reg_map.get(reg, Decimal("0.00"))
                if delta > 0:
                    reg_deltas.append({"region": reg, "delta": delta})
            reg_deltas.sort(key=lambda x: x['delta'], reverse=True)
            top_regions = [{"region": x["region"], "delta": serialize_decimal(x["delta"])} for x in reg_deltas[:3]]

            # Resource deltas
            c_res = base_records.filter(usage_start__date__gte=start_curr, usage_start__date__lte=end_curr, currency=curr_db_val).values('resource_id', 'resource_name').annotate(total=Sum('cost'))
            p_res = base_records.filter(usage_start__date__gte=start_prev, usage_start__date__lte=end_prev, currency=curr_db_val).values('resource_id', 'resource_name').annotate(total=Sum('cost'))
            
            def group_res(agg_list):
                res_map = {}
                for item in agg_list:
                    r_id = (item['resource_id'] or '').strip()
                    r_name = (item['resource_name'] or '').strip()
                    cost = item['total'] or Decimal("0.00")
                    key = f"id:{r_id}" if r_id else (f"name:{r_name}" if r_name else "unknown")
                    res_map[key] = res_map.get(key, Decimal("0.00")) + cost
                return res_map
                
            c_res_map = group_res(c_res)
            p_res_map = group_res(p_res)
            
            all_res_keys = set(c_res_map.keys()).union(set(p_res_map.keys()))
            res_deltas = []
            for rk in all_res_keys:
                delta = c_res_map.get(rk, Decimal("0.00")) - p_res_map.get(rk, Decimal("0.00"))
                if delta > 0:
                    res_deltas.append({"resource_key": rk, "delta": delta})
            res_deltas.sort(key=lambda x: x['delta'], reverse=True)
            top_resources = [{"resource_key": x["resource_key"], "delta": serialize_decimal(x["delta"])} for x in res_deltas[:3]]

            contributors[curr] = {
                "top_services": top_services,
                "top_regions": top_regions,
                "top_resources": top_resources
            }

        return {
            "current_period": {"start": start_curr.isoformat(), "end": end_curr.isoformat()},
            "previous_period": {"start": start_prev.isoformat(), "end": end_prev.isoformat()},
            "comparison_is_equivalent_duration": comparison_is_equivalent_duration,
            "currency_comparisons": currency_comparisons,
            "contributors": contributors
        }

    return {"results": []}
