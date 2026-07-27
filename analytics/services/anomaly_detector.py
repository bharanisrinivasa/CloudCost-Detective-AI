from decimal import Decimal
import math
import datetime
from django.db import models
from django.db.models import Sum
from django.db.models.functions import TruncDate
from billing.models import BillingRecord, BillingUpload
from analytics.models import CostAnomaly

# Central Configuration Thresholds
MIN_HISTORY_DAYS = 7
ABS_Z_SCORE_THRESHOLD = 2.0
MIN_SPIKE_PCT_THRESHOLD = 50.0
MIN_DAILY_COST = Decimal("10.00")
MIN_RESOURCE_COST = Decimal("5.00")
MIN_GROWTH_PCT_THRESHOLD = 50.0
MIN_GROWTH_ABSOLUTE_INCREASE = Decimal("20.00")

def classify_severity(z_score, deviation_pct, cost_impact):
    """
    Deterministically classify severity based on cost impact and statistical evidence.
    z_score can be None for zero standard deviation anomalies.
    """
    if not isinstance(cost_impact, Decimal):
        cost_impact = Decimal(str(cost_impact))
    if not isinstance(deviation_pct, Decimal):
        deviation_pct = Decimal(str(deviation_pct))
    
    # 1. CRITICAL:
    # Requires significant cost impact AND strong statistical deviation
    if cost_impact >= Decimal("500.00") and (z_score is None or z_score >= 3.0 or deviation_pct >= Decimal("200.0")):
        return "CRITICAL"
        
    # 2. HIGH:
    # Moderate cost impact with strong stats OR high cost impact with weak stats
    if (cost_impact >= Decimal("100.00") and (z_score is None or z_score >= 2.5 or deviation_pct >= Decimal("100.0"))) or cost_impact >= Decimal("500.00"):
        return "HIGH"
        
    # 3. MEDIUM:
    # Visible cost impact with basic variance OR moderate cost impact with weak stats
    if (cost_impact >= Decimal("30.00") and (z_score is None or z_score >= 2.0 or deviation_pct >= Decimal("50.0"))) or cost_impact >= Decimal("100.00"):
        return "MEDIUM"
        
    return "LOW"

def calculate_stats(observations):
    """
    Calculate Mean and Standard Deviation from a list of Decimal values.
    """
    if not observations:
        return Decimal("0.00"), Decimal("0.00")
    n = len(observations)
    mean = sum(observations) / Decimal(str(n))
    if n < 2:
        return mean, Decimal("0.00")
    
    # Calculate sample variance
    variance = sum((x - mean) ** 2 for x in observations) / Decimal(str(n - 1))
    std_dev = Decimal(str(math.sqrt(float(variance))))
    return mean, std_dev

def get_single_upload_for_date(date_val, records_qs):
    """
    Find unique BillingUpload ID for records on a specific date.
    Returns None if records span multiple uploads.
    """
    upload_ids = list(
        records_qs.filter(usage_start__date=date_val)
        .values_list('upload_id', flat=True)
        .distinct()
    )
    if len(upload_ids) == 1:
        return upload_ids[0]
    return None

def run_anomaly_detection_for_user(user):
    """
    Backward compatibility wrapper for user-based scans.
    """
    from accounts.models import Project
    project = Project.objects.filter(organization__memberships__user=user).first()
    return run_anomaly_detection_for_project(project, actor_user=user)

def run_anomaly_detection_for_project(project, actor_user=None):
    """
    Run synchronous anomaly detection scans for a project.
    Calculates historical baselines dynamically to prevent look-ahead bias.
    """
    if not project:
        return {
            'created': 0,
            'skipped': 0,
            'updated': 0,
            'message': 'No project specified for anomaly detection.'
        }

    records = BillingRecord.objects.filter(upload__project=project).exclude(usage_start__isnull=True)
    
    # Get sorted list of distinct dates
    db_dates = list(
        records.annotate(date=TruncDate('usage_start'))
        .values_list('date', flat=True)
        .distinct()
        .order_by('date')
    )
    db_dates = [d for d in db_dates if d is not None]
    
    results = {
        'created': 0,
        'skipped': 0,
        'updated': 0,
        'message': ''
    }
    
    if len(db_dates) < MIN_HISTORY_DAYS + 1:
        results['message'] = f"Insufficient data: at least {MIN_HISTORY_DAYS + 1} dates required, found {len(db_dates)}."
        return results
        
    # --- 1. DAILY SPIKE ANOMALY DETECTION ---
    daily_costs_qs = list(
        records.annotate(date=TruncDate('usage_start'))
        .values('date')
        .annotate(total=Sum('cost'))
    )
    daily_cost_map = {item['date']: Decimal(str(item['total'] or "0.00")) for item in daily_costs_qs if item['date']}
    
    for i in range(MIN_HISTORY_DAYS, len(db_dates)):
        d_i = db_dates[i]
        preceding_dates = db_dates[0:i]
        preceding_costs = [daily_cost_map[d] for d in preceding_dates]
        
        mean, std_dev = calculate_stats(preceding_costs)
        actual_cost = daily_cost_map[d_i]
        cost_impact = actual_cost - mean
        
        is_anomaly = False
        z_score = None
        
        if std_dev > 0:
            z_score = float(actual_cost - mean) / float(std_dev)
            if z_score >= ABS_Z_SCORE_THRESHOLD:
                is_anomaly = True
        else:
            if actual_cost > mean:
                is_anomaly = True
                
        if is_anomaly:
            deviation_pct = Decimal("0.00") if mean == 0 else (cost_impact / mean) * 100
            if deviation_pct >= MIN_SPIKE_PCT_THRESHOLD and cost_impact >= MIN_DAILY_COST:
                sev = classify_severity(z_score, deviation_pct, cost_impact)
                upload_id = get_single_upload_for_date(d_i, records)
                
                desc = f"Daily spending of {actual_cost:.2f} was {deviation_pct:.1f}% above historical baseline average of {mean:.2f}."
                
                # Deduplicate or update
                anomaly_qs = CostAnomaly.objects.filter(
                    project=project,
                    anomaly_type="DAILY_SPIKE",
                    detected_date=d_i,
                    service_name="",
                    resource_id="",
                    resource_name=""
                )
                if anomaly_qs.exists():
                    anomaly = anomaly_qs.first()
                    if anomaly.status == "OPEN":
                        anomaly.actual_cost = actual_cost
                        anomaly.expected_cost = mean
                        anomaly.deviation_percentage = deviation_pct
                        anomaly.z_score = z_score
                        anomaly.severity = sev
                        anomaly.description = desc
                        anomaly.billing_upload_id = upload_id
                        anomaly.user = actor_user
                        anomaly.save()
                        results['updated'] += 1
                    else:
                        results['skipped'] += 1
                else:
                    CostAnomaly.objects.create(
                        project=project,
                        user=actor_user,
                        anomaly_type="DAILY_SPIKE",
                        detected_date=d_i,
                        service_name="",
                        resource_id="",
                        resource_name="",
                        actual_cost=actual_cost,
                        expected_cost=mean,
                        deviation_percentage=deviation_pct,
                        z_score=z_score,
                        severity=sev,
                        description=desc,
                        billing_upload_id=upload_id,
                        status="OPEN"
                    )
                    results['created'] += 1
 
    # --- 2. SERVICE SPIKE ANOMALY DETECTION ---
    services = list(records.values_list('service', flat=True).distinct())
    normalized_services = []
    for s in services:
        s_norm = (s or '').strip()
        if not s_norm:
            s_norm = "Unknown Service"
        if s_norm not in normalized_services:
            normalized_services.append(s_norm)
            
    for svc in normalized_services:
        if svc == "Unknown Service":
            service_records = records.filter(models.Q(service__isnull=True) | models.Q(service=""))
        else:
            service_records = records.filter(service=svc)
            
        svc_dates = list(
            service_records.annotate(date=TruncDate('usage_start'))
            .values_list('date', flat=True)
            .distinct()
            .order_by('date')
        )
        svc_dates = [d for d in svc_dates if d is not None]
        
        if len(svc_dates) < MIN_HISTORY_DAYS + 1:
            continue
            
        svc_costs_qs = list(
            service_records.annotate(date=TruncDate('usage_start'))
            .values('date')
            .annotate(total=Sum('cost'))
        )
        svc_cost_map = {item['date']: Decimal(str(item['total'] or "0.00")) for item in svc_costs_qs if item['date']}
        
        for j in range(MIN_HISTORY_DAYS, len(svc_dates)):
            d_j = svc_dates[j]
            preceding_dates = svc_dates[0:j]
            preceding_costs = [svc_cost_map[d] for d in preceding_dates]
            
            mean, std_dev = calculate_stats(preceding_costs)
            actual_cost = svc_cost_map[d_j]
            cost_impact = actual_cost - mean
            
            is_anomaly = False
            z_score = None
            
            if std_dev > 0:
                z_score = float(actual_cost - mean) / float(std_dev)
                if z_score >= ABS_Z_SCORE_THRESHOLD:
                    is_anomaly = True
            else:
                if actual_cost > mean:
                    is_anomaly = True
                    
            if is_anomaly:
                deviation_pct = Decimal("0.00") if mean == 0 else (cost_impact / mean) * 100
                if deviation_pct >= MIN_SPIKE_PCT_THRESHOLD and cost_impact >= MIN_DAILY_COST:
                    sev = classify_severity(z_score, deviation_pct, cost_impact)
                    upload_id = get_single_upload_for_date(d_j, service_records)
                    
                    regions = list(service_records.filter(usage_start__date=d_j).values_list('region', flat=True).distinct())
                    reg_val = regions[0] if len(regions) == 1 else ""
                    
                    desc = f"Service '{svc}' cost of {actual_cost:.2f} was {deviation_pct:.1f}% above historical baseline average of {mean:.2f}."
                    
                    anomaly_qs = CostAnomaly.objects.filter(
                        project=project,
                        anomaly_type="SERVICE_SPIKE",
                        detected_date=d_j,
                        service_name=svc,
                        resource_id="",
                        resource_name=""
                    )
                    if anomaly_qs.exists():
                        anomaly = anomaly_qs.first()
                        if anomaly.status == "OPEN":
                            anomaly.actual_cost = actual_cost
                            anomaly.expected_cost = mean
                            anomaly.deviation_percentage = deviation_pct
                            anomaly.z_score = z_score
                            anomaly.severity = sev
                            anomaly.region = reg_val
                            anomaly.description = desc
                            anomaly.billing_upload_id = upload_id
                            anomaly.user = actor_user
                            anomaly.save()
                            results['updated'] += 1
                        else:
                            results['skipped'] += 1
                    else:
                        CostAnomaly.objects.create(
                            project=project,
                            user=actor_user,
                            anomaly_type="SERVICE_SPIKE",
                            detected_date=d_j,
                            service_name=svc,
                            resource_id="",
                            resource_name="",
                            region=reg_val,
                            actual_cost=actual_cost,
                            expected_cost=mean,
                            deviation_percentage=deviation_pct,
                            z_score=z_score,
                            severity=sev,
                            description=desc,
                            billing_upload_id=upload_id,
                            status="OPEN"
                        )
                        results['created'] += 1
 
    # --- 3. RESOURCE SPIKE ANOMALY DETECTION ---
    raw_resources = list(records.values('resource_id', 'resource_name').distinct())
    
    for res_item in raw_resources:
        r_id = (res_item['resource_id'] or '').strip()
        r_name = (res_item['resource_name'] or '').strip()
        
        # Skip if both missing
        if not r_id and not r_name:
            continue
            
        if r_id:
            res_records = records.filter(resource_id=r_id)
        else:
            res_records = records.filter(
                models.Q(resource_id__isnull=True) | models.Q(resource_id=""),
                resource_name=r_name
            )
            
        res_dates = list(
            res_records.annotate(date=TruncDate('usage_start'))
            .values_list('date', flat=True)
            .distinct()
            .order_by('date')
        )
        res_dates = [d for d in res_dates if d is not None]
        
        if len(res_dates) < MIN_HISTORY_DAYS + 1:
            continue
            
        res_costs_qs = list(
            res_records.annotate(date=TruncDate('usage_start'))
            .values('date')
            .annotate(total=Sum('cost'))
        )
        res_cost_map = {item['date']: Decimal(str(item['total'] or "0.00")) for item in res_costs_qs if item['date']}
        
        for k in range(MIN_HISTORY_DAYS, len(res_dates)):
            d_k = res_dates[k]
            preceding_dates = res_dates[0:k]
            preceding_costs = [res_cost_map[d] for d in preceding_dates]
            
            mean, std_dev = calculate_stats(preceding_costs)
            actual_cost = res_cost_map[d_k]
            cost_impact = actual_cost - mean
            
            is_anomaly = False
            z_score = None
            
            if std_dev > 0:
                z_score = float(actual_cost - mean) / float(std_dev)
                if z_score >= ABS_Z_SCORE_THRESHOLD:
                    is_anomaly = True
            else:
                if actual_cost > mean:
                    is_anomaly = True
                    
            if is_anomaly:
                deviation_pct = Decimal("0.00") if mean == 0 else (cost_impact / mean) * 100
                if deviation_pct >= MIN_SPIKE_PCT_THRESHOLD and cost_impact >= MIN_RESOURCE_COST:
                    sev = classify_severity(z_score, deviation_pct, cost_impact)
                    upload_id = get_single_upload_for_date(d_k, res_records)
                    
                    services_found = list(res_records.filter(usage_start__date=d_k).values_list('service', flat=True).distinct())
                    svc_val = services_found[0] if len(services_found) == 1 else ""
                    
                    regions = list(res_records.filter(usage_start__date=d_k).values_list('region', flat=True).distinct())
                    reg_val = regions[0] if len(regions) == 1 else ""
                    
                    display_name = r_name or r_id
                    desc = f"Resource '{display_name}' cost of {actual_cost:.2f} was {deviation_pct:.1f}% above historical baseline average of {mean:.2f}."
                    
                    anomaly_qs = CostAnomaly.objects.filter(
                        project=project,
                        anomaly_type="RESOURCE_SPIKE",
                        detected_date=d_k,
                        service_name=svc_val,
                        resource_id=r_id,
                        resource_name=r_name
                    )
                    if anomaly_qs.exists():
                        anomaly = anomaly_qs.first()
                        if anomaly.status == "OPEN":
                            anomaly.actual_cost = actual_cost
                            anomaly.expected_cost = mean
                            anomaly.deviation_percentage = deviation_pct
                            anomaly.z_score = z_score
                            anomaly.severity = sev
                            anomaly.region = reg_val
                            anomaly.description = desc
                            anomaly.billing_upload_id = upload_id
                            anomaly.user = actor_user
                            anomaly.save()
                            results['updated'] += 1
                        else:
                            results['skipped'] += 1
                    else:
                        CostAnomaly.objects.create(
                            project=project,
                            user=actor_user,
                            anomaly_type="RESOURCE_SPIKE",
                            detected_date=d_k,
                            service_name=svc_val,
                            resource_id=r_id,
                            resource_name=r_name,
                            region=reg_val,
                            actual_cost=actual_cost,
                            expected_cost=mean,
                            deviation_percentage=deviation_pct,
                            z_score=z_score,
                            severity=sev,
                            description=desc,
                            billing_upload_id=upload_id,
                            status="OPEN"
                        )
                        results['created'] += 1
 
    # --- 4. UNUSUAL GROWTH ANOMALY DETECTION ---
    for i in range(MIN_HISTORY_DAYS, len(db_dates)):
        d_i = db_dates[i]
        d_prev = db_dates[i - 1]
        
        current_cost = daily_cost_map[d_i]
        previous_cost = daily_cost_map[d_prev]
        
        if previous_cost > 0:
            growth_pct = ((current_cost - previous_cost) / previous_cost) * 100
            absolute_increase = current_cost - previous_cost
            
            if growth_pct >= MIN_GROWTH_PCT_THRESHOLD and absolute_increase >= MIN_GROWTH_ABSOLUTE_INCREASE:
                # We classify growth severity without Z-score (passing None)
                sev = classify_severity(None, growth_pct, absolute_increase)
                upload_id = get_single_upload_for_date(d_i, records)
                
                desc = f"Day-over-day cost increased by {absolute_increase:.2f} ({growth_pct:.1f}% growth) from {previous_cost:.2f} to {current_cost:.2f}."
                
                anomaly_qs = CostAnomaly.objects.filter(
                    project=project,
                    anomaly_type="UNUSUAL_GROWTH",
                    detected_date=d_i,
                    service_name="",
                    resource_id="",
                    resource_name=""
                )
                if anomaly_qs.exists():
                    anomaly = anomaly_qs.first()
                    if anomaly.status == "OPEN":
                        anomaly.actual_cost = current_cost
                        anomaly.expected_cost = previous_cost
                        anomaly.deviation_percentage = growth_pct
                        anomaly.z_score = None
                        anomaly.severity = sev
                        anomaly.description = desc
                        anomaly.billing_upload_id = upload_id
                        anomaly.user = actor_user
                        anomaly.save()
                        results['updated'] += 1
                    else:
                        results['skipped'] += 1
                else:
                    CostAnomaly.objects.create(
                        project=project,
                        user=actor_user,
                        anomaly_type="UNUSUAL_GROWTH",
                        detected_date=d_i,
                        service_name="",
                        resource_id="",
                        resource_name="",
                        actual_cost=current_cost,
                        expected_cost=previous_cost,
                        deviation_percentage=growth_pct,
                        z_score=None,
                        severity=sev,
                        description=desc,
                        billing_upload_id=upload_id,
                        status="OPEN"
                    )
                    results['created'] += 1
 
    return results
