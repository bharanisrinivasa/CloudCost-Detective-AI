from decimal import Decimal
import datetime
from django.utils import timezone
from collections import defaultdict
from billing.models import BillingRecord
from ai_engine.models import Recommendation

def resolve_period_dates(period, start_date_str=None, end_date_str=None):
    """
    Resolves the date range for a given simulation period based on the local timezone.
    Returns (start_date, end_date) as datetime.date objects.
    """
    today = timezone.localdate()
    if period == "CURRENT_MONTH":
        start_date = today.replace(day=1)
        # Find the last day of the current month
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

def calculate_baseline(user, start_date, end_date):
    """
    Backward compatibility wrapper for user baseline cost calculations.
    """
    from accounts.models import Project
    project = Project.objects.filter(organization__memberships__user=user).first()
    return calculate_baseline_for_project(project, start_date, end_date)

def calculate_baseline_for_project(project, start_date, end_date):
    """
    Aggregates project baseline costs directly from BillingRecord objects.
    """
    if not project:
        return defaultdict(lambda: defaultdict(Decimal)), set()
    records = BillingRecord.objects.filter(
        upload__project=project
    ).exclude(usage_start__isnull=True)
    
    baseline_costs = defaultdict(lambda: defaultdict(Decimal))
    encountered_currencies = set()
    
    for r in records:
        usage_dt = r.usage_start
        if timezone.is_aware(usage_dt):
            usage_dt = timezone.localtime(usage_dt)
        usage_date = usage_dt.date()
        
        if start_date <= usage_date <= end_date:
            curr = (r.currency or "").strip()
            if not curr:
                curr = "UNKNOWN"
            encountered_currencies.add(curr)
            
            service = (r.service or "").strip()
            if not service:
                service = "Unknown Service"
                
            baseline_costs[curr][service] += r.cost
            
    return baseline_costs, encountered_currencies


def run_cost_simulation(user, period, start_date_str=None, end_date_str=None, actions=None):
    """
    DEPRECATED: Backward compatibility wrapper for user-based cost simulation.
    Use run_cost_simulation_for_project instead.
    """
    import warnings
    warnings.warn(
        "run_cost_simulation is deprecated. Use run_cost_simulation_for_project instead.",
        DeprecationWarning,
        stacklevel=2
    )
    from accounts.models import Project
    project = Project.objects.filter(organization__memberships__user=user).first()
    return run_cost_simulation_for_project(project, period, start_date_str, end_date_str, actions, actor_user=user)

def run_cost_simulation_for_project(project, period, start_date_str=None, end_date_str=None, actions=None, actor_user=None):
    """
    Executes a deterministic What-if Cost Simulation based on project historical billing records.
    Calculations use pure Decimal arithmetic with negative flooring and independent currency scaling.
    """
    if not actions:
        raise ValueError("At least one simulation action is required.")
        
    start_date, end_date = resolve_period_dates(period, start_date_str, end_date_str)
    
    # Recalculate baseline values
    baseline_costs, encountered_currencies = calculate_baseline_for_project(project, start_date, end_date)
    
    if not encountered_currencies:
        raise ValueError("No billing records found for the selected time period.")
        
    # Track uniqueness constraints
    seen_manual_targets = set()  # (service, currency)
    seen_recommendations = set()  # recommendation_id
    seen_service_currency_from_recs = set()  # (service, currency)
    
    validated_actions = []
    
    for act in actions:
        act_type = act.get("action_type")
        if act_type not in ["PERCENT_DECREASE", "PERCENT_INCREASE", "FIXED_DECREASE", "FIXED_INCREASE", "RECOMMENDATION_SAVINGS"]:
            raise ValueError(f"Invalid action type: {act_type}")
            
        if act_type == "RECOMMENDATION_SAVINGS":
            rec_id = act.get("recommendation_id")
            if not rec_id:
                raise ValueError("Recommendation ID is required for RECOMMENDATION_SAVINGS action.")
                
            if rec_id in seen_recommendations:
                raise ValueError(f"Duplicate recommendation ID detected: {rec_id}")
            seen_recommendations.add(rec_id)
            
            try:
                rec = Recommendation.objects.get(pk=rec_id, project=project)
            except Recommendation.DoesNotExist:
                raise ValueError("Recommendation not found or unauthorized.")
                
            if rec.estimated_monthly_savings is None:
                raise ValueError("Selected recommendation has no estimated monthly savings.")
            if rec.estimated_monthly_savings <= Decimal("0.00"):
                raise ValueError("Selected recommendation must have estimated monthly savings greater than 0.")
                
            rec_currency = (rec.currency or "").strip()
            if not rec_currency:
                rec_currency = "UNKNOWN"
                
            rec_service = (rec.service_name or "").strip()
            if not rec_service:
                rec_service = "Unknown Service"
                
            # Verify the service and currency combination actually exists in the user's baseline
            if rec_currency not in baseline_costs or rec_service not in baseline_costs[rec_currency]:
                raise ValueError(f"Recommendation targets service '{rec_service}' in currency '{rec_currency}', which is not present in the baseline data.")
                
            # Conflict checks
            service_currency_key = (rec_service, rec_currency)
            if service_currency_key in seen_manual_targets:
                raise ValueError(f"Conflict: Action targeting service '{rec_service}' in currency '{rec_currency}' already has a manual action.")
            if service_currency_key in seen_service_currency_from_recs:
                raise ValueError(f"Conflict: Action targeting service '{rec_service}' in currency '{rec_currency}' already has a recommendation action.")
            seen_service_currency_from_recs.add(service_currency_key)
            
            validated_actions.append({
                "type": "recommendation",
                "recommendation_id": rec_id,
                "service": rec_service,
                "currency": rec_currency,
                "action_type": act_type,
                "value": rec.estimated_monthly_savings,
                "description": f"Apply Recommendation #{rec_id} savings for {rec_service} ({rec_currency})"
            })
            
        else:
            # Manual action
            service = (act.get("service") or "").strip()
            if not service:
                service = "Unknown Service"
                
            # Validate that the service is valid for this project
            user_has_service = BillingRecord.objects.filter(
                upload__project=project, service=service
            ).exists()
            if not user_has_service and service != "Unknown Service":
                raise ValueError(f"Service '{service}' is not a valid service for this user.")
                
            raw_value = act.get("value")
            if raw_value is None:
                raise ValueError("Value is required for manual actions.")
            try:
                val = Decimal(str(raw_value))
            except Exception:
                raise ValueError(f"Invalid numeric value: {raw_value}")
                
            if act_type in ["PERCENT_DECREASE", "PERCENT_INCREASE"]:
                if not (Decimal("0.00") <= val <= Decimal("1000.00")):
                    raise ValueError("Percentage must be between 0 and 1000.")
            elif act_type in ["FIXED_DECREASE", "FIXED_INCREASE"]:
                if not (Decimal("0.00") <= val <= Decimal("10000000.00")):
                    raise ValueError("Fixed amount must be between 0 and 10,000,000.")
                    
            raw_currency = act.get("currency")
            if raw_currency:
                raw_currency = raw_currency.strip()
                
            if act_type in ["FIXED_DECREASE", "FIXED_INCREASE"]:
                if not raw_currency:
                    raise ValueError("Currency is required for manual fixed actions.")
                action_currency = raw_currency if raw_currency else "UNKNOWN"
                
                # Validate service has records in this currency
                if action_currency not in baseline_costs or service not in baseline_costs[action_currency]:
                    raise ValueError(f"Selected service '{service}' does not have billing records in currency '{action_currency}' for the selected period.")
                    
                # Conflict checks
                key = (service, action_currency)
                if key in seen_manual_targets:
                    raise ValueError(f"Duplicate manual action for service '{service}' and currency '{action_currency}'.")
                if key in seen_service_currency_from_recs:
                    raise ValueError(f"Conflict: Action targeting service '{service}' in currency '{action_currency}' conflicts with a recommendation action.")
                seen_manual_targets.add(key)
                
                validated_actions.append({
                    "type": "manual",
                    "service": service,
                    "currency": action_currency,
                    "action_type": act_type,
                    "value": val,
                    "description": f"Manual {act_type.replace('_', ' ').lower()}: {val} {action_currency} on {service}"
                })
            else:
                # Percentage changes
                if raw_currency:
                    action_currency = raw_currency
                    key = (service, action_currency)
                    if key in seen_manual_targets:
                        raise ValueError(f"Duplicate manual action for service '{service}' and currency '{action_currency}'.")
                    if key in seen_service_currency_from_recs:
                        raise ValueError(f"Conflict: Action targeting service '{service}' in currency '{action_currency}' conflicts with a recommendation action.")
                    seen_manual_targets.add(key)
                    
                    validated_actions.append({
                        "type": "manual",
                        "service": service,
                        "currency": action_currency,
                        "action_type": act_type,
                        "value": val,
                        "description": f"Manual {act_type.replace('_', ' ').lower()}: {val}% on {service} ({action_currency})"
                    })
                else:
                    # Apply to all currencies where this service exists in this project's general billing records
                    all_user_currencies = list(BillingRecord.objects.filter(
                        upload__project=project, service=service
                    ).values_list("currency", flat=True).distinct())
                    all_user_currencies = [c.strip() if c else "UNKNOWN" for c in all_user_currencies]
                    if not all_user_currencies:
                        all_user_currencies = ["UNKNOWN"]
                        
                    for action_currency in all_user_currencies:
                        key = (service, action_currency)
                        if key in seen_manual_targets:
                            raise ValueError(f"Duplicate manual action for service '{service}' and currency '{action_currency}'.")
                        if key in seen_service_currency_from_recs:
                            raise ValueError(f"Conflict: Action targeting service '{service}' in currency '{action_currency}' conflicts with a recommendation action.")
                        seen_manual_targets.add(key)
                        
                        validated_actions.append({
                            "type": "manual",
                            "service": service,
                            "currency": action_currency,
                            "action_type": act_type,
                            "value": val,
                            "description": f"Manual {act_type.replace('_', ' ').lower()}: {val}% on {service} ({action_currency})"
                        })

    # Execute simulation
    currency_results = {}
    for curr in sorted(list(encountered_currencies)):
        # Calculate overall baseline for this currency
        services_data = baseline_costs[curr]
        baseline_total = sum(services_data.values())
        
        # Clone service baseline costs to simulate modifications
        simulated_services = dict(services_data)
        
        actions_applied_for_currency = []
        
        for act in validated_actions:
            if act["currency"] != curr:
                continue
                
            svc = act["service"]
            act_type = act["action_type"]
            val = act["value"]
            
            # Fetch baseline cost for targeted service (default to 0)
            orig_cost = simulated_services.get(svc, Decimal("0.00"))
            
            if act_type == "PERCENT_DECREASE":
                change = orig_cost * val / Decimal("100")
                simulated_services[svc] = max(Decimal("0.00"), orig_cost - change)
            elif act_type == "PERCENT_INCREASE":
                change = orig_cost * val / Decimal("100")
                simulated_services[svc] = orig_cost + change
            elif act_type == "FIXED_DECREASE":
                simulated_services[svc] = max(Decimal("0.00"), orig_cost - val)
            elif act_type == "FIXED_INCREASE":
                simulated_services[svc] = orig_cost + val
            elif act_type == "RECOMMENDATION_SAVINGS":
                simulated_services[svc] = max(Decimal("0.00"), orig_cost - val)
                
            actions_applied_for_currency.append(act)

        # Sum simulated service costs (which are individual non-negative)
        simulated_total = sum(simulated_services.values())
        
        # Calculate result metrics
        absolute_change = simulated_total - baseline_total
        
        if baseline_total > Decimal("0.00"):
            percentage_change = (absolute_change / baseline_total) * Decimal("100")
            percentage_change_reason = None
        else:
            percentage_change = None
            percentage_change_reason = "ZERO_BASELINE"
            
        estimated_savings = max(Decimal("0.00"), baseline_total - simulated_total)
        
        currency_results[curr] = {
            "currency": curr,
            "baseline_cost": baseline_total.quantize(Decimal("0.01")),
            "simulated_cost": simulated_total.quantize(Decimal("0.01")),
            "absolute_change": absolute_change.quantize(Decimal("0.01")),
            "percentage_change": percentage_change.quantize(Decimal("0.01")) if percentage_change is not None else None,
            "percentage_change_reason": percentage_change_reason,
            "estimated_savings": estimated_savings.quantize(Decimal("0.01")),
            "actions_applied": actions_applied_for_currency
        }

    return {
        "period": period,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
        "currency_results": currency_results,
        "has_multiple_currencies": len(currency_results) > 1,
        "simulation_disclaimer": "This is a hypothetical cost simulation based on historical billing data. It does not modify OCI resources or guarantee future savings."
    }
