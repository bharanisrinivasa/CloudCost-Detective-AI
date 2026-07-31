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
    Backward compatibility wrapper for user-based scans.
    """
    from accounts.models import Project
    project = Project.objects.filter(organization__memberships__user=user).first()
    return run_waste_detection_for_project(project, actor_user=user)

def run_waste_detection_for_project(project, actor_user=None):
    """
    Synchronously scan project's BillingRecord database entries for potential waste.
    Saves or updates findings in a secure, idempotent manner.
    """
    if not project:
        return {
            'created': 0,
            'updated': 0,
            'analyzed': 0,
            'potential_savings': {}
        }

    records = BillingRecord.objects.filter(upload__project=project).exclude(usage_start__isnull=True)
    
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
                project=project,
                waste_type=fd['type'],
                resource_key=r_key,
                service_name=service_name,
                currency=currency,
                defaults={
                    'user': actor_user,
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

    # ----------------------------------------------------
    # OCI TELEMETRY & INVENTORY EXTENSION
    # ----------------------------------------------------
    from oci_connector.models import (
        OCIConnection,
        OCIComputeInstance,
        OCIVolume,
        OCIObjectStorageBucket,
        OCIPublicIp,
        OCILoadBalancer,
        OCIResourceMetricSummary
    )
    
    oci_conn = OCIConnection.objects.filter(project=project, is_active=True).first()
    if oci_conn:
        today = datetime.date.today()
        
        # 1. Detached Volumes
        detached_vols = OCIVolume.objects.filter(
            project=project,
            connection=oci_conn,
            inventory_status="PRESENT",
            state="AVAILABLE",
            attachment_state="DETACHED"
        )
        for vol in detached_vols:
            vol_cost_est = Decimal(str(vol.size_in_gbs)) * Decimal("0.05")  # 5 cents per GB/month estimate
            evidence = (
                f"OCI inventory/attachment data observed the volume without an attachment during the latest successful authoritative scan. "
                f"This volume is currently unattached and incurring charges. size: {vol.size_in_gbs} GB."
            )
            finding, created = WasteFinding.objects.update_or_create(
                project=project,
                waste_type="DETACHED_VOLUME",
                resource_key=f"id:{vol.ocid}",
                service_name="Storage",
                currency="USD",
                defaults={
                    'user': actor_user,
                    'resource_id': vol.ocid,
                    'resource_name': vol.name,
                    'region': vol.region,
                    'first_seen': vol.created_at.date() if vol.created_at else today,
                    'last_seen': today,
                    'observation_days': 1,
                    'calendar_span_days': 1,
                    'coverage_ratio': Decimal("1.0"),
                    'total_cost': vol_cost_est,
                    'average_daily_cost': vol_cost_est / Decimal("30"),
                    'estimated_monthly_cost': vol_cost_est,
                    'estimated_monthly_savings': vol_cost_est,
                    'confidence': "HIGH",
                    'evidence': evidence
                }
            )
            if created:
                results['created'] += 1
            else:
                results['updated'] += 1
            if finding.status == "OPEN":
                results['potential_savings']["USD"] = results['potential_savings'].get("USD", Decimal("0.00")) + vol_cost_est

        # 2. Idle Compute Candidate
        IDLE_COMPUTE_MIN_DAYS = 7
        IDLE_COMPUTE_MAX_AVG_CPU = Decimal("5.00")
        MIN_METRIC_COVERAGE_RATIO = Decimal("0.80")
        
        running_vms = OCIComputeInstance.objects.filter(
            project=project,
            connection=oci_conn,
            inventory_status="PRESENT",
            state="RUNNING"
        )
        for vm in running_vms:
            end_date = today
            start_date = today - datetime.timedelta(days=IDLE_COMPUTE_MIN_DAYS)
            metrics = OCIResourceMetricSummary.objects.filter(
                project=project,
                resource_id=vm.ocid,
                metric_name="CpuUtilization",
                date__gte=start_date,
                date__lte=end_date
            )
            if len(metrics) < IDLE_COMPUTE_MIN_DAYS:
                continue
                
            if any(m.coverage_ratio is None or m.coverage_ratio < MIN_METRIC_COVERAGE_RATIO for m in metrics):
                continue
                
            if any(m.average_value is None for m in metrics):
                continue

            avg_cpu = sum(m.average_value for m in metrics) / len(metrics)
            max_cpu = max(m.maximum_value for m in metrics) if any(m.maximum_value is not None for m in metrics) else None
            
            if avg_cpu >= IDLE_COMPUTE_MAX_AVG_CPU:
                continue
            if max_cpu is not None and max_cpu > Decimal("50.00"):
                continue

            rx_metrics = OCIResourceMetricSummary.objects.filter(
                project=project, resource_id=vm.ocid, metric_name="NetworksBytesIn",
                date__gte=start_date, date__lte=end_date
            )
            tx_metrics = OCIResourceMetricSummary.objects.filter(
                project=project, resource_id=vm.ocid, metric_name="NetworksBytesOut",
                date__gte=start_date, date__lte=end_date
            )
            
            network_detail = ""
            has_network_telemetry = (
                len(rx_metrics) >= IDLE_COMPUTE_MIN_DAYS and
                len(tx_metrics) >= IDLE_COMPUTE_MIN_DAYS and
                all(m.average_value is not None and m.coverage_ratio is not None and m.coverage_ratio >= MIN_METRIC_COVERAGE_RATIO for m in rx_metrics) and
                all(m.average_value is not None and m.coverage_ratio is not None and m.coverage_ratio >= MIN_METRIC_COVERAGE_RATIO for m in tx_metrics)
            )

            if has_network_telemetry:
                avg_rx = sum(m.average_value for m in rx_metrics) / len(rx_metrics)
                avg_tx = sum(m.average_value for m in tx_metrics) / len(tx_metrics)
                network_detail = f" Network traffic shows minimal activity (Avg Rx: {avg_rx:.2f} bytes/sec, Avg Tx: {avg_tx:.2f} bytes/sec)."
            else:
                network_detail = " Network metrics are unavailable or have insufficient coverage."

            evidence = (
                f"OCI monitoring data confirms low observed CPU activity (Average CPU: {avg_cpu:.2f}%, Max CPU: {max_cpu if max_cpu is not None else 'N/A'}%)"
                f" over {IDLE_COMPUTE_MIN_DAYS} observed days.{network_detail} "
                f"Review VM shape and application workload before considering rightsizing or shutdown."
            )

            vm_cost_est = Decimal("20.00")
            confidence = "HIGH" if has_network_telemetry else "MEDIUM"
            
            finding, created = WasteFinding.objects.update_or_create(
                project=project,
                waste_type="IDLE_COMPUTE_CANDIDATE",
                resource_key=f"id:{vm.ocid}",
                service_name="Compute",
                currency="USD",
                defaults={
                    'user': actor_user,
                    'resource_id': vm.ocid,
                    'resource_name': vm.name,
                    'region': vm.region,
                    'first_seen': start_date,
                    'last_seen': end_date,
                    'observation_days': len(metrics),
                    'calendar_span_days': IDLE_COMPUTE_MIN_DAYS,
                    'coverage_ratio': Decimal("1.0"),
                    'total_cost': vm_cost_est,
                    'average_daily_cost': vm_cost_est / Decimal("30"),
                    'estimated_monthly_cost': vm_cost_est,
                    'estimated_monthly_savings': vm_cost_est,
                    'confidence': confidence,
                    'evidence': evidence
                }
            )
            if created:
                results['created'] += 1
            else:
                results['updated'] += 1
            if finding.status == "OPEN":
                results['potential_savings']["USD"] = results['potential_savings'].get("USD", Decimal("0.00")) + vm_cost_est

        # 3. Possible Unassigned Public IP
        orphan_ips = OCIPublicIp.objects.filter(
            project=project,
            connection=oci_conn,
            inventory_status="PRESENT",
            lifecycle_state="AVAILABLE",
            is_orphan=True
        )
        for ip in orphan_ips:
            ip_cost_est = Decimal("7.20")
            evidence = (
                f"OCI inventory reports this reserved public IP without an observed assignment during the latest successful inventory scan."
            )
            finding, created = WasteFinding.objects.update_or_create(
                project=project,
                waste_type="POSSIBLE_UNASSIGNED_PUBLIC_IP",
                resource_key=f"id:{ip.ocid}",
                service_name="Networking",
                currency="USD",
                defaults={
                    'user': actor_user,
                    'resource_id': ip.ocid,
                    'resource_name': ip.ip_address,
                    'region': ip.region,
                    'first_seen': ip.created_at.date() if ip.created_at else today,
                    'last_seen': today,
                    'observation_days': 1,
                    'calendar_span_days': 1,
                    'coverage_ratio': Decimal("1.0"),
                    'total_cost': ip_cost_est,
                    'average_daily_cost': ip_cost_est / Decimal("30"),
                    'estimated_monthly_cost': ip_cost_est,
                    'estimated_monthly_savings': ip_cost_est,
                    'confidence': "HIGH",
                    'evidence': evidence
                }
            )
            if created:
                results['created'] += 1
            else:
                results['updated'] += 1
            if finding.status == "OPEN":
                results['potential_savings']["USD"] = results['potential_savings'].get("USD", Decimal("0.00")) + ip_cost_est

        # 4. Possible Empty Storage Buckets
        empty_buckets = OCIObjectStorageBucket.objects.filter(
            project=project,
            connection=oci_conn,
            inventory_status="PRESENT",
            approximate_count=0
        )
        for b in empty_buckets:
            evidence = (
                f"OCI Object Storage check confirms bucket '{b.name}' in namespace '{b.namespace}' "
                f"has approximate object count of authoritatively 0. This bucket is empty. "
                f"Review usage patterns before considering cleanup."
            )
            finding, created = WasteFinding.objects.update_or_create(
                project=project,
                waste_type="POSSIBLE_EMPTY_BUCKET",
                resource_key=f"name:{b.namespace}:{b.name}",
                service_name="Object Storage",
                currency="USD",
                defaults={
                    'user': actor_user,
                    'resource_id': b.name,
                    'resource_name': b.name,
                    'region': b.region,
                    'first_seen': b.created_at.date() if b.created_at else today,
                    'last_seen': today,
                    'observation_days': 1,
                    'calendar_span_days': 1,
                    'coverage_ratio': Decimal("1.0"),
                    'total_cost': Decimal("0.00"),
                    'average_daily_cost': Decimal("0.00"),
                    'estimated_monthly_cost': Decimal("0.00"),
                    'estimated_monthly_savings': Decimal("0.00"),
                    'confidence': "HIGH",
                    'evidence': evidence
                }
            )
            if created:
                results['created'] += 1
            else:
                results['updated'] += 1
            if finding.status == "OPEN":
                results['potential_savings']["USD"] = results['potential_savings'].get("USD", Decimal("0.00")) + Decimal("0.00")

        # 5. Idle Load Balancers
        idle_lbs = OCILoadBalancer.objects.filter(
            project=project,
            connection=oci_conn,
            inventory_status="PRESENT",
            state="ACTIVE"
        )
        for lb in idle_lbs:
            lb_metrics = OCIResourceMetricSummary.objects.filter(
                project=project,
                resource_id=lb.ocid,
                metric_name="ActiveConnections",
                date__gte=today - datetime.timedelta(days=7),
                date__lte=today
            )
            if (len(lb_metrics) >= 7 and
                all(m.average_value is not None and m.coverage_ratio is not None and m.coverage_ratio >= MIN_METRIC_COVERAGE_RATIO for m in lb_metrics) and
                all(m.average_value == Decimal("0.00") for m in lb_metrics)):
                
                lb_cost_est = Decimal("15.00")
                evidence = (
                    f"OCI load balancer check shows active connections count was average zero over the last 7 days. "
                    f"Load balancer is active but idle. Verify traffic requirements before stopping or deleting."
                )
                finding, created = WasteFinding.objects.update_or_create(
                    project=project,
                    waste_type="IDLE_LOAD_BALANCER_CANDIDATE",
                    resource_key=f"id:{lb.ocid}",
                    service_name="Networking",
                    currency="USD",
                    defaults={
                        'user': actor_user,
                        'resource_id': lb.ocid,
                        'resource_name': lb.name,
                        'region': lb.region,
                        'first_seen': today - datetime.timedelta(days=7),
                        'last_seen': today,
                        'observation_days': len(lb_metrics),
                        'calendar_span_days': 7,
                        'coverage_ratio': Decimal("1.0"),
                        'total_cost': lb_cost_est,
                        'average_daily_cost': lb_cost_est / Decimal("30"),
                        'estimated_monthly_cost': lb_cost_est,
                        'estimated_monthly_savings': lb_cost_est,
                        'confidence': "HIGH",
                        'evidence': evidence
                    }
                )
                if created:
                    results['created'] += 1
                else:
                    results['updated'] += 1
                if finding.status == "OPEN":
                    results['potential_savings']["USD"] = results['potential_savings'].get("USD", Decimal("0.00")) + lb_cost_est

    return results
