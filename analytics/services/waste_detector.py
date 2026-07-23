from decimal import Decimal
import math
import datetime
from django.db import models
from django.db.models import Sum, Min, Max, Count
from django.db.models.functions import TruncDate
from billing.models import BillingRecord
from analytics.models import WasteFinding

# Central Configurable Thresholds & Savings Factors
MIN_OBSERVATION_DAYS = 7
MIN_COVERAGE_RATIO = Decimal("0.70")

MIN_AVERAGE_DAILY_COST = Decimal("0.10")
MAX_LOW_DAILY_COST = Decimal("5.00")

MIN_TOTAL_COST_FOR_FINDING = Decimal("5.00")
MIN_ESTIMATED_MONTHLY_SAVINGS = Decimal("2.00")

PERSISTENT_LOW_COST_SAVINGS_FACTOR = Decimal("0.50")
DORMANT_COST_SAVINGS_FACTOR = Decimal("0.80")
STALE_RESOURCE_SAVINGS_FACTOR = Decimal("0.60")
POSSIBLE_UNUSED_STORAGE_SAVINGS_FACTOR = Decimal("0.50")

# Centralized allowlist for DORMANT_COST_PATTERN
DORMANT_ALLOWLIST = {
    "Compute": ["OCPU-Hours", "GB-Hours", "Hours"],
    "Storage": ["GB-Months", "GB"],
    "Block Volume": ["GB-Months", "GB"],
    "Object Storage": ["GB-Months", "GB", "Requests"],
    "Database": ["Hours", "OCPU-Hours"],
}

def calculate_std_dev(values, mean):
    if not values:
        return 0.0
    n = len(values)
    mean_val = float(mean)
    variance = sum((float(x) - mean_val) ** 2 for x in values) / n
    return math.sqrt(variance)

def run_waste_detection_for_user(user):
    """
    Synchronously scan user's BillingRecord database entries for potential waste.
    Saves or updates findings in a secure, idempotent manner.
    """
    records = BillingRecord.objects.filter(upload__uploaded_by=user).exclude(usage_start__isnull=True)
    
    # Aggregate daily cost and usage per resource in the database to prevent loading raw millions
    resource_daily_qs = (
        records.annotate(date=TruncDate('usage_start'))
        .values('resource_id', 'resource_name', 'service', 'region', 'currency', 'date')
        .annotate(
            daily_cost=Sum('cost'),
            daily_usage=Sum('usage_quantity'),
        )
    )
    
    # Retrieve unique usage units per resource to check compatibility
    resource_units_qs = (
        records.values('resource_id', 'resource_name', 'service', 'currency')
        .annotate(unique_units=Count('usage_unit', distinct=True))
    )
    
    # Store unique units mapping
    resource_units_map = {}
    for item in records.values('resource_id', 'resource_name', 'service', 'currency', 'usage_unit').distinct():
        r_id = (item['resource_id'] or '').strip()
        r_name = (item['resource_name'] or '').strip()
        service = (item['service'] or '').strip()
        currency = item['currency']
        
        # Build matching key
        if r_id:
            r_key = f"id:{r_id}"
        elif r_name:
            r_key = f"name:{r_name}"
        else:
            continue
            
        group_key = (r_key, service, currency)
        if group_key not in resource_units_map:
            resource_units_map[group_key] = set()
        if item['usage_unit']:
            resource_units_map[group_key].add(item['usage_unit'])

    # Group daily records in Python
    resources = {}
    for item in resource_daily_qs:
        r_id = (item['resource_id'] or '').strip()
        r_name = (item['resource_name'] or '').strip()
        service = (item['service'] or '').strip()
        region = (item['region'] or '').strip()
        currency = item['currency']
        date_val = item['date']
        daily_cost = item['daily_cost'] or Decimal("0.00")
        daily_usage = item['daily_usage'] or Decimal("0.000000")
        
        if r_id:
            r_key = f"id:{r_id}"
        elif r_name:
            r_key = f"name:{r_name}"
        else:
            # Skip if both missing
            continue
            
        group_key = (r_key, service, currency)
        if group_key not in resources:
            resources[group_key] = {
                'resource_key': r_key,
                'resource_id': r_id,
                'resource_name': r_name,
                'service_name': service,
                'region': region,
                'currency': currency,
                'daily_data': []
            }
        resources[group_key]['daily_data'].append({
            'date': date_val,
            'cost': daily_cost,
            'usage': daily_usage
        })

    results = {
        'created': 0,
        'updated': 0,
        'analyzed': len(resources),
        'potential_savings': {} # currency -> Decimal total
    }

    # Evaluate each resource group
    for group_key, res in resources.items():
        # Sort daily entries by date
        res['daily_data'].sort(key=lambda x: x['date'])
        
        daily_data = res['daily_data']
        first_seen = daily_data[0]['date']
        last_seen = daily_data[-1]['date']
        observed_days = len(daily_data)
        calendar_span_days = (last_seen - first_seen).days + 1
        
        # Calculate coverage ratio
        coverage_ratio = Decimal(observed_days) / Decimal(calendar_span_days)
        
        # Rule 4: Minimum observation days (fewer than 7 observed dates skipped)
        if observed_days < MIN_OBSERVATION_DAYS or calendar_span_days < MIN_OBSERVATION_DAYS:
            continue
            
        total_cost = sum(item['cost'] for item in daily_data)
        average_daily_cost = total_cost / Decimal(observed_days)
        estimated_monthly_cost = average_daily_cost * Decimal("30")
        
        # Rule 7: Daily cost range check parameters
        # (Needed primarily for PERSISTENT_LOW_COST_RESOURCE, but total cost check applies generally)
        if total_cost < MIN_TOTAL_COST_FOR_FINDING:
            continue
            
        r_key = res['resource_key']
        r_id = res['resource_id']
        r_name = res['resource_name']
        service_name = res['service_name']
        region = res['region']
        currency = res['currency']
        
        # Get usage units list for this resource group
        units = list(resource_units_map.get(group_key, set()))
        units_consistent = len(units) == 1
        usage_unit = units[0] if units_consistent else ""
        
        # Statistical variances
        cost_std_dev = calculate_std_dev([item['cost'] for item in daily_data], average_daily_cost)
        cost_variance_pct = (cost_std_dev / float(average_daily_cost)) if average_daily_cost > 0 else 0.0
        
        usage_values = [item['usage'] for item in daily_data]
        mean_usage = sum(usage_values) / len(usage_values) if usage_values else Decimal("0.0")
        usage_std_dev = calculate_std_dev(usage_values, mean_usage)
        usage_variance_pct = (usage_std_dev / float(mean_usage)) if mean_usage > 0 else 0.0

        # Check each detector rule:
        findings_to_check = []

        # ----------------------------------------------------
        # DETECTOR A: PERSISTENT_LOW_COST_RESOURCE
        # ----------------------------------------------------
        if MIN_AVERAGE_DAILY_COST <= average_daily_cost <= MAX_LOW_DAILY_COST and coverage_ratio >= MIN_COVERAGE_RATIO:
            savings = estimated_monthly_cost * PERSISTENT_LOW_COST_SAVINGS_FACTOR
            if savings >= MIN_ESTIMATED_MONTHLY_SAVINGS:
                # Determine confidence
                if observed_days >= 14 and coverage_ratio >= Decimal("0.95") and cost_variance_pct < 0.01:
                    conf = "HIGH"
                else:
                    conf = "MEDIUM"
                
                evidence = (
                    f"Potential optimization candidate (persistent low-cost activity observed over {observed_days} days). "
                    f"Average daily cost is {average_daily_cost:.2f} {currency} inside low-cost daily threshold. "
                    f"Requires OCI resource-state verification."
                )
                findings_to_check.append({
                    'type': 'PERSISTENT_LOW_COST_RESOURCE',
                    'savings': savings,
                    'confidence': conf,
                    'evidence': evidence
                })

        # ----------------------------------------------------
        # DETECTOR B: DORMANT_COST_PATTERN
        # ----------------------------------------------------
        # Check if units are consistent and in allowlist
        if service_name in DORMANT_ALLOWLIST and units_consistent and usage_unit in DORMANT_ALLOWLIST[service_name]:
            total_usage = sum(item['usage'] for item in daily_data)
            # If usage quantity is zero or near zero (e.g. < 1.00 total)
            if total_usage < Decimal("1.00") and coverage_ratio >= MIN_COVERAGE_RATIO:
                savings = estimated_monthly_cost * DORMANT_COST_SAVINGS_FACTOR
                if savings >= MIN_ESTIMATED_MONTHLY_SAVINGS:
                    if observed_days >= 14 and coverage_ratio >= Decimal("0.95") and cost_variance_pct < 0.01:
                        conf = "HIGH"
                    else:
                        conf = "MEDIUM"
                        
                    evidence = (
                        f"Possible unused resource (cost accrued with low/zero recorded usage quantity). "
                        f"Total observed usage is {total_usage:.2f} {usage_unit} over {observed_days} days. "
                        f"Requires OCI resource-state verification."
                    )
                    findings_to_check.append({
                        'type': 'DORMANT_COST_PATTERN',
                        'savings': savings,
                        'confidence': conf,
                        'evidence': evidence
                    })

        # ----------------------------------------------------
        # DETECTOR C: STALE_RESOURCE_COST
        # ----------------------------------------------------
        # Cost variance must be extremely low (< 1% standard deviation of mean)
        if coverage_ratio >= Decimal("0.80") and cost_variance_pct < 0.01:
            # Check usage variance if unit compatible
            usage_stale = False
            has_valid_usage_metrics = False
            if service_name in DORMANT_ALLOWLIST and units_consistent and usage_unit in DORMANT_ALLOWLIST[service_name]:
                has_valid_usage_metrics = True
                if mean_usage == 0 or usage_variance_pct < 0.01:
                    usage_stale = True
            
            # If compatible usage metrics exist but usage is volatile: DO NOT create STALE_RESOURCE_COST.
            should_create_stale = False
            if has_valid_usage_metrics:
                if usage_stale:
                    should_create_stale = True
            else:
                should_create_stale = True
                
            if should_create_stale:
                savings = estimated_monthly_cost * STALE_RESOURCE_SAVINGS_FACTOR
                if savings >= MIN_ESTIMATED_MONTHLY_SAVINGS:
                    # Set confidence
                    # For cost-only stale findings, cap confidence at MEDIUM.
                    # Do not assign HIGH confidence without validated stable usage evidence.
                    if has_valid_usage_metrics and observed_days >= 14 and coverage_ratio >= Decimal("0.95"):
                        conf = "HIGH"
                    else:
                        conf = "MEDIUM"
                    
                    if has_valid_usage_metrics:
                        evidence = (
                            f"Potential optimization candidate (recurring stable-cost review candidate). "
                            f"The resource has an unchanging daily billing cost (average {average_daily_cost:.2f} {currency}) "
                            f"and usage quantity (average {mean_usage:.2f} {usage_unit}) over {observed_days} observed days. "
                            f"Requires OCI resource-state verification."
                        )
                    else:
                        evidence = (
                            f"Potential optimization candidate (recurring stable-cost review candidate). "
                            f"The resource has an unchanging daily billing cost (average {average_daily_cost:.2f} {currency}) "
                            f"over {observed_days} observed days with usage metrics unavailable/incompatible. "
                            f"Requires OCI resource-state verification."
                        )
                    
                    findings_to_check.append({
                        'type': 'STALE_RESOURCE_COST',
                        'savings': savings,
                        'confidence': conf,
                        'evidence': evidence
                    })

        # ----------------------------------------------------
        # DETECTOR D: POSSIBLE_UNUSED_STORAGE
        # ----------------------------------------------------
        svc_lower = service_name.lower().replace(" ", "")
        is_storage_service = "storage" in svc_lower or "volume" in svc_lower or "filesystem" in svc_lower
        
        # Build allowed storage units matching key in DORMANT_ALLOWLIST
        allowed_storage_units = []
        for k, v in DORMANT_ALLOWLIST.items():
            k_lower = k.lower().replace(" ", "")
            if "storage" in k_lower or "volume" in k_lower or "filesystem" in k_lower:
                allowed_storage_units.extend(v)
                
        is_unit_valid_storage = units_consistent and usage_unit and (usage_unit in allowed_storage_units)
        
        if is_storage_service and coverage_ratio >= MIN_COVERAGE_RATIO and cost_variance_pct < 0.01 and is_unit_valid_storage:
            total_usage = sum(item['usage'] for item in daily_data)
            is_usage_stable = (mean_usage == 0 or total_usage < Decimal("1.00") or usage_variance_pct < 0.01)
            
            if is_usage_stable:
                savings = estimated_monthly_cost * POSSIBLE_UNUSED_STORAGE_SAVINGS_FACTOR
                if savings >= MIN_ESTIMATED_MONTHLY_SAVINGS:
                    conf = "MEDIUM"
                    evidence = (
                        f"Storage resource shows stable recurring billing with low or unchanged recorded usage under a validated billing unit. "
                        f"This is a potential optimization candidate and requires OCI resource-state verification."
                    )
                    findings_to_check.append({
                        'type': 'POSSIBLE_UNUSED_STORAGE',
                        'savings': savings,
                        'confidence': conf,
                        'evidence': evidence
                    })

        # Insert or update findings
        for fd in findings_to_check:
            finding, created = WasteFinding.objects.update_or_create(
                user=user,
                waste_type=fd['type'],
                resource_key=r_key,
                service_name=service_name,
                currency=currency,
                defaults={
                    'resource_id': r_id,
                    'resource_name': r_name,  # Updated latest name if changed
                    'region': region,
                    'first_seen': first_seen,
                    'last_seen': last_seen,
                    'observation_days': observed_days,
                    'calendar_span_days': calendar_span_days,
                    'coverage_ratio': coverage_ratio,
                    'total_cost': total_cost,
                    'average_daily_cost': average_daily_cost,
                    'estimated_monthly_cost': estimated_monthly_cost,
                    'estimated_monthly_savings': fd['savings'],
                    'confidence': fd['confidence'],
                    'evidence': fd['evidence']
                }
            )
            if created:
                results['created'] += 1
            else:
                results['updated'] += 1
                
            # Aggregate savings per currency for open findings
            if finding.status == "OPEN":
                results['potential_savings'][currency] = results['potential_savings'].get(currency, Decimal("0.00")) + fd['savings']

    return results
