import datetime
import hashlib
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from ai_engine.models import Recommendation
from analytics.models import WasteFinding, CostAnomaly
from billing.models import BillingRecord


def classify_service(service_string: str) -> str:
    """Normalize OCI billing services into Compute, Database, Storage, Backup, or Unknown."""
    s = service_string.lower().strip()
    if any(k in s for k in ["backup", "snapshot", "recovery"]):
        return "BACKUP"
    if any(k in s for k in ["compute", "virtual machine", "dedicated vm", "instance"]):
        return "COMPUTE"
    if any(k in s for k in ["database", "autonomous", "mysql", "nosql"]):
        return "DATABASE"
    if any(k in s for k in ["objectstorage", "blockstorage", "filestorage", "volume", "storage"]):
        return "STORAGE"
    return "UNKNOWN"


def generate_fingerprint(project_id, rec_type, scope, src_type, src_id, identity_type, identity_value, service, region, currency) -> str:
    """Generate a deterministic SHA-256 fingerprint hash for a recommendation."""
    raw_str = f"{project_id}|{rec_type}|{scope}|{src_type or ''}|{src_id or ''}|{identity_type}|{identity_value}|{service}|{region}|{currency}"
    return hashlib.sha256(raw_str.encode('utf-8')).hexdigest()


def generate_explanation_hash(rec) -> str:
    """Generate an explanation hash representing all factors that materially affect the Gemini explanation."""
    raw_str = (
        f"{rec.recommendation_type}|{rec.recommendation_scope}|{rec.identity_type}|{rec.identity_value}|"
        f"{rec.service_name}|{rec.region}|{rec.current_monthly_cost or ''}|{rec.estimated_monthly_savings or ''}|"
        f"{rec.currency}|{rec.confidence}|{rec.priority}|{rec.evidence}|{rec.recommended_action}|{rec.limitations}"
    )
    return hashlib.sha256(raw_str.encode('utf-8')).hexdigest()


def calculate_stats(decimal_list):
    """Calculate mean and standard deviation safely using Decimals."""
    if not decimal_list:
        return Decimal("0.0000"), Decimal("0.0000")
    mean = sum(decimal_list) / Decimal(len(decimal_list))
    variance = sum((x - mean) ** 2 for x in decimal_list) / Decimal(len(decimal_list))
    std_dev = variance.sqrt().quantize(Decimal("0.0001"))
    mean_dec = mean.quantize(Decimal("0.0001"))
    return mean_dec, std_dev


def run_recommendation_engine(user):
    """
    DEPRECATED: Backward compatibility wrapper for user-based recommendation run.
    Use run_recommendation_engine_for_project instead.
    """
    import warnings
    warnings.warn(
        "run_recommendation_engine is deprecated. Use run_recommendation_engine_for_project instead.",
        DeprecationWarning,
        stacklevel=2
    )
    from accounts.models import Project
    project = Project.objects.filter(organization__memberships__user=user).first()
    return run_recommendation_engine_for_project(project, actor_user=user)

def run_recommendation_engine_for_project(project, actor_user=None):
    """Run the deterministic recommendation engine for a specific project and return the count generated."""
    if not project:
        return 0
    # Find latest billing record to anchor our 30-day window
    latest_record = BillingRecord.objects.filter(upload__project=project).order_by('-usage_start').first()
    if latest_record and latest_record.usage_start:
        latest_date = latest_record.usage_start.date()
    else:
        latest_date = datetime.date.today()

    start_date = latest_date - datetime.timedelta(days=29)
    end_date = latest_date
    start_dt = datetime.datetime.combine(start_date, datetime.time.min)
    end_dt = datetime.datetime.combine(end_date, datetime.time.max)

    generated_count = 0

    with transaction.atomic():
        # ----------------------------------------------------
        # 1. RIGHTSIZE_REVIEW & STORAGE_OPTIMIZATION (from WasteFinding)
        # ----------------------------------------------------
        open_waste = WasteFinding.objects.filter(project=project, status="OPEN")
        for wf in open_waste:
            normalized_service = classify_service(wf.service_name)
            
            # Determine recommendation type
            if wf.waste_type in ["PERSISTENT_LOW_COST_RESOURCE", "DORMANT_COST_PATTERN"]:
                rec_type = "RIGHTSIZE_REVIEW"
                rec_action = (
                    "Review the current OCI configuration and validate CPU, memory, storage, "
                    "and workload requirements before considering a smaller configuration."
                )
                limitation_text = (
                    "No OCI utilization telemetry (CPU, RAM, network) is available. "
                    "This recommendation is based entirely on historical billing patterns. "
                    "Validate actual workload utilization before changing instances."
                )
            elif wf.waste_type == "IDLE_COMPUTE_CANDIDATE":
                rec_type = "RIGHTSIZE_REVIEW"
                rec_action = "Review VM shape and application workload before considering rightsizing or shutdown."
                limitation_text = (
                    "OCI monitoring data confirms low observed CPU activity during the analyzed period. "
                    "Review workload requirements, memory demand, network behavior, and availability requirements "
                    "before resizing or stopping the resource."
                )
            elif wf.waste_type == "DETACHED_VOLUME":
                rec_type = "STORAGE_OPTIMIZATION"
                rec_action = "Verify if this volume is no longer needed. Consider creating a backup and deleting it if it is obsolete."
                limitation_text = "Verify attachment state in OCI Console. Storage volume is reported detached."
            elif wf.waste_type == "POSSIBLE_UNASSIGNED_PUBLIC_IP":
                rec_type = "COST_PATTERN_REVIEW"
                rec_action = "Review if this public IP can be released. Unassigned reserved public IPs accrue hourly charges."
                limitation_text = "Verify that the IP is not needed for future dynamic allocations."
            elif wf.waste_type == "POSSIBLE_EMPTY_BUCKET":
                rec_type = "STORAGE_OPTIMIZATION"
                rec_action = "Review if this Object Storage bucket is obsolete. If empty and unused, consider removing it."
                limitation_text = "Object Storage count is verified as zero."
            elif wf.waste_type == "IDLE_LOAD_BALANCER_CANDIDATE":
                rec_type = "RIGHTSIZE_REVIEW"
                rec_action = "Verify if this load balancer is still in use. It has zero recorded connections over the last 7 days."
                limitation_text = "Review traffic metrics and connection details before terminating."
            elif wf.waste_type in ["POSSIBLE_UNUSED_STORAGE", "STALE_RESOURCE_COST"]:
                rec_type = "STORAGE_OPTIMIZATION"
                rec_action = (
                    "Review whether this storage resource is still required. Check OCI lifecycle "
                    "policies and tiering options (e.g., Object Storage Archive) to optimize costs."
                )
                limitation_text = (
                    "This recommendation is based on suspected unutilized or stale storage costs. "
                    "Verify if the volume or bucket is actively in use before modifying lifecycle rules."
                )
            else:
                continue

            # Determine identity
            resource_id_trimmed = wf.resource_id.strip() if wf.resource_id else ""
            resource_name_trimmed = wf.resource_name.strip() if wf.resource_name else ""
            
            if resource_id_trimmed:
                identity_type = "id"
                identity_value = resource_id_trimmed
            elif resource_name_trimmed:
                identity_type = "name"
                identity_value = resource_name_trimmed
            else:
                identity_type = "unknown"
                identity_value = ""

            fingerprint = generate_fingerprint(
                project.id, rec_type, "RESOURCE", "WASTE_FINDING", wf.id,
                identity_type, identity_value, normalized_service, wf.region or "UNKNOWN", wf.currency
            )

            # Determine priority and confidence
            confidence = wf.confidence
            savings = wf.estimated_monthly_savings or Decimal("0.00")
            if savings > Decimal("500.00") and confidence == "HIGH":
                priority = "CRITICAL"
            elif savings > Decimal("100.00"):
                priority = "HIGH"
            elif savings > Decimal("20.00"):
                priority = "MEDIUM"
            else:
                priority = "LOW"

            waste_type_dict = dict(wf.WASTE_TYPE_CHOICES)
            waste_display = waste_type_dict.get(wf.waste_type, wf.waste_type)

            evidence_text = (
                f"Source Waste Finding: {waste_display}.\n"
                f"Observation Span: {wf.observation_days} days. Total observed cost: {wf.total_cost} {wf.currency}.\n"
                f"Savings Estimate Source: Waste finding heuristics."
            )

            # Update or create preserving user workflow status
            rec, created = Recommendation.objects.get_or_create(
                project=project,
                fingerprint=fingerprint,
                defaults={
                    "user": actor_user,
                    "recommendation_type": rec_type,
                    "recommendation_scope": "RESOURCE",
                    "resource_id": resource_id_trimmed,
                    "resource_name": resource_name_trimmed,
                    "identity_type": identity_type,
                    "identity_value": identity_value,
                    "service_name": normalized_service,
                    "region": wf.region or "UNKNOWN",
                    "source_type": "WASTE_FINDING",
                    "source_id": wf.id,
                    "current_monthly_cost": wf.estimated_monthly_cost,
                    "estimated_monthly_savings": wf.estimated_monthly_savings,
                    "currency": wf.currency,
                    "savings_source": "WASTE_FINDING",
                    "confidence": confidence,
                    "priority": priority,
                    "evidence": evidence_text,
                    "recommended_action": rec_action,
                    "limitations": limitation_text,
                    "status": "OPEN",
                }
            )

            if not created:
                # Update deterministic attributes only
                rec.current_monthly_cost = wf.estimated_monthly_cost
                rec.estimated_monthly_savings = wf.estimated_monthly_savings
                rec.confidence = confidence
                rec.priority = priority
                rec.evidence = evidence_text
                rec.recommended_action = rec_action
                rec.limitations = limitation_text
                rec.user = actor_user
                
                # Check cache hash to mark stale if needed
                new_hash = generate_explanation_hash(rec)
                if rec.ai_explanation_hash != new_hash:
                    rec.ai_explanation_hash = ""
                    rec.ai_explanation_json = None
                rec.save()

            generated_count += 1

        # ----------------------------------------------------
        # 2. RESERVED_CAPACITY_REVIEW (from BillingRecord pattern)
        # ----------------------------------------------------
        # Query compute/database spends over the last 30 calendar dates
        billing_records = BillingRecord.objects.filter(
            upload__project=project,
            usage_start__gte=start_dt,
            usage_start__lte=end_dt
        )

        # Build in-memory daily aggregation grouped by (service_name, region, currency, date)
        daily_spend = {}
        for r in billing_records:
            normalized_service = classify_service(r.service)
            if normalized_service not in ["COMPUTE", "DATABASE"]:
                continue
            
            reg = r.region or "UNKNOWN"
            curr = r.currency or "UNKNOWN"
            day = r.usage_start.date()
            
            key = (normalized_service, reg, curr)
            if key not in daily_spend:
                daily_spend[key] = {}
            if day not in daily_spend[key]:
                daily_spend[key][day] = Decimal("0.00")
            daily_spend[key][day] += r.cost

        for key, dates in daily_spend.items():
            service, region, currency = key
            
            observed_days = len(dates)
            coverage_ratio = Decimal(str(observed_days)) / Decimal("30.00")
            
            # Check thresholds
            if observed_days < 24 or coverage_ratio < Decimal("0.80"):
                continue
            
            costs = list(dates.values())
            mean_spend, std_dev = calculate_stats(costs)
            
            if mean_spend <= Decimal("0.00"):
                continue
                
            cov = std_dev / mean_spend
            if cov > Decimal("0.20"):
                continue

            # This service/region qualifies for Reserved Capacity review!
            rec_type = "RESERVED_CAPACITY_REVIEW"
            rec_action = (
                "Review applicable OCI commitment or reserved-capacity pricing options "
                "and workload longevity before making a commitment."
            )
            limitation_text = (
                "No pricing calculations, commitment percentages, or break-even horizons "
                "can be computed deterministically without custom contract pricing data. "
                "Review expected workload longevity and applicable OCI commitment terms before making a commitment."
            )

            # Cumulative monthly cost in 30 days
            monthly_cost = sum(costs)

            fingerprint = generate_fingerprint(
                project.id, rec_type, "SERVICE_REGION", "BILLING_PATTERN", None,
                "unknown", "", service, region, currency
            )

            evidence_text = (
                f"Observed unique days with spend: {observed_days}/30.\n"
                f"Mean daily spend: {mean_spend:.2f} {currency}. Standard Deviation: {std_dev:.2f} {currency}.\n"
                f"Coefficient of Variation (stability metric): {cov:.4f}."
            )

            # Determine priority and confidence
            # Reserved Capacity cannot automatically become HIGH confidence
            confidence = "MEDIUM"
            if monthly_cost > Decimal("1000.00"):
                priority = "HIGH"
            elif monthly_cost > Decimal("200.00"):
                priority = "MEDIUM"
            else:
                priority = "LOW"

            rec, created = Recommendation.objects.get_or_create(
                project=project,
                fingerprint=fingerprint,
                defaults={
                    "user": actor_user,
                    "recommendation_type": rec_type,
                    "recommendation_scope": "SERVICE_REGION",
                    "resource_id": "",
                    "resource_name": "",
                    "identity_type": "unknown",
                    "identity_value": "",
                    "service_name": service,
                    "region": region,
                    "source_type": "BILLING_PATTERN",
                    "source_id": None,
                    "current_monthly_cost": monthly_cost,
                    "estimated_monthly_savings": None,
                    "currency": currency,
                    "savings_source": "NONE",
                    "confidence": confidence,
                    "priority": priority,
                    "evidence": evidence_text,
                    "recommended_action": rec_action,
                    "limitations": limitation_text,
                    "status": "OPEN",
                }
            )

            if not created:
                rec.current_monthly_cost = monthly_cost
                rec.confidence = confidence
                rec.priority = priority
                rec.evidence = evidence_text
                rec.recommended_action = rec_action
                rec.limitations = limitation_text
                rec.user = actor_user
                
                new_hash = generate_explanation_hash(rec)
                if rec.ai_explanation_hash != new_hash:
                    rec.ai_explanation_hash = ""
                    rec.ai_explanation_json = None
                rec.save()

            generated_count += 1

        # ----------------------------------------------------
        # 3. BACKUP_POLICY_REVIEW (from BillingRecord pattern)
        # ----------------------------------------------------
        # Centralized backup matcher tokens
        backup_tokens = [
            "boot volume backup",
            "block volume backup",
            "volume backup",
            "backup",
            "snapshot",
            "recovery"
        ]

        # Scan for backup records in the last 30 days
        backup_records = []
        for r in billing_records:
            s_low = r.service.lower()
            rn_low = r.resource_name.lower()
            if any(t in s_low or t in rn_low for t in backup_tokens):
                backup_records.append(r)

        # Group cumulative cost in the 30-day window by resource identifier & currency
        resource_backup_costs = {}
        for r in backup_records:
            normalized_service = classify_service(r.service)
            reg = r.region or "UNKNOWN"
            curr = r.currency or "UNKNOWN"
            res_id = r.resource_id.strip() if r.resource_id else ""
            res_name = r.resource_name.strip() if r.resource_name else ""
            
            if res_id:
                identity_type = "id"
                identity_value = res_id
            elif res_name:
                identity_type = "name"
                identity_value = res_name
            else:
                identity_type = "unknown"
                identity_value = ""

            key = (res_id, res_name, identity_type, identity_value, normalized_service, reg, curr)
            if key not in resource_backup_costs:
                resource_backup_costs[key] = Decimal("0.00")
            resource_backup_costs[key] += r.cost

        for key, total_cost in resource_backup_costs.items():
            res_id, res_name, identity_type, identity_value, service, region, currency = key
            
            # Match backup review threshold
            if total_cost < Decimal("10.00"):
                continue

            rec_type = "BACKUP_POLICY_REVIEW"
            rec_action = (
                "Review backup retention requirements, lifecycle configuration, "
                "storage tier options, and compliance requirements before changing the backup policy."
            )
            limitation_text = (
                "No automatic backup deletion or OCI transitions are proposed. "
                "Backup retention policies must satisfy business SLA and compliance standards "
                "before manual remediation is carried out."
            )

            fingerprint = generate_fingerprint(
                project.id, rec_type, "RESOURCE", "BILLING_PATTERN", None,
                identity_type, identity_value, service, region, currency
            )

            evidence_text = f"Cumulative backup billing over the last 30 days is {total_cost:.2f} {currency}."

            # Determine priority and confidence
            confidence = "LOW"
            if total_cost > Decimal("100.00"):
                priority = "MEDIUM"
            else:
                priority = "LOW"

            rec, created = Recommendation.objects.get_or_create(
                project=project,
                fingerprint=fingerprint,
                defaults={
                    "user": actor_user,
                    "recommendation_type": rec_type,
                    "recommendation_scope": "RESOURCE",
                    "resource_id": res_id,
                    "resource_name": res_name,
                    "identity_type": identity_type,
                    "identity_value": identity_value,
                    "service_name": service,
                    "region": region,
                    "source_type": "BILLING_PATTERN",
                    "source_id": None,
                    "current_monthly_cost": total_cost,
                    "estimated_monthly_savings": None,
                    "currency": currency,
                    "savings_source": "NONE",
                    "confidence": confidence,
                    "priority": priority,
                    "evidence": evidence_text,
                    "recommended_action": rec_action,
                    "limitations": limitation_text,
                    "status": "OPEN",
                }
            )

            if not created:
                rec.current_monthly_cost = total_cost
                rec.confidence = confidence
                rec.priority = priority
                rec.evidence = evidence_text
                rec.recommended_action = rec_action
                rec.limitations = limitation_text
                rec.user = actor_user
                
                new_hash = generate_explanation_hash(rec)
                if rec.ai_explanation_hash != new_hash:
                    rec.ai_explanation_hash = ""
                    rec.ai_explanation_json = None
                rec.save()

            generated_count += 1

        # ----------------------------------------------------
        # 4. COST_PATTERN_REVIEW (from CostAnomaly)
        # ----------------------------------------------------
        open_anomalies = CostAnomaly.objects.filter(project=project, status="OPEN", severity__in=["HIGH", "CRITICAL"])
        for ca in open_anomalies:
            rec_type = "COST_PATTERN_REVIEW"
            rec_action = (
                "A significant cost anomaly was detected. Investigate the resource and "
                "billing spike details to identify the cause of the cost increase."
            )
            limitation_text = (
                "Anomaly event spikes do not represent stable long-term waste patterns. "
                "Investigate OCI audit logs for recent deployments or workload changes."
            )

            resource_id_trimmed = ca.resource_id.strip() if ca.resource_id else ""
            resource_name_trimmed = ca.resource_name.strip() if ca.resource_name else ""
            
            if resource_id_trimmed:
                identity_type = "id"
                identity_value = resource_id_trimmed
            elif resource_name_trimmed:
                identity_type = "name"
                identity_value = resource_name_trimmed
            else:
                identity_type = "unknown"
                identity_value = ""

            normalized_service = classify_service(ca.service_name)

            fingerprint = generate_fingerprint(
                project.id, rec_type, "RESOURCE", "COST_ANOMALY", ca.id,
                identity_type, identity_value, normalized_service, ca.region or "UNKNOWN", "USD"
            )

            # Anomaly cost is NOT current_monthly_cost
            evidence_text = (
                f"High-Severity Cost Anomaly detected on {ca.detected_date}.\n"
                f"Actual Cost: {ca.actual_cost} USD vs Expected: {ca.expected_cost} USD.\n"
                f"Spike Deviation: {ca.deviation_percentage}%."
            )

            confidence = "HIGH" if ca.severity == "CRITICAL" else "MEDIUM"
            priority = "CRITICAL" if ca.severity == "CRITICAL" else "HIGH"

            rec, created = Recommendation.objects.get_or_create(
                project=project,
                fingerprint=fingerprint,
                defaults={
                    "user": actor_user,
                    "recommendation_type": rec_type,
                    "recommendation_scope": "RESOURCE",
                    "resource_id": resource_id_trimmed,
                    "resource_name": resource_name_trimmed,
                    "identity_type": identity_type,
                    "identity_value": identity_value,
                    "service_name": normalized_service,
                    "region": ca.region or "UNKNOWN",
                    "source_type": "COST_ANOMALY",
                    "source_id": ca.id,
                    "current_monthly_cost": None,
                    "estimated_monthly_savings": None,
                    "currency": "USD",
                    "savings_source": "NONE",
                    "confidence": confidence,
                    "priority": priority,
                    "evidence": evidence_text,
                    "recommended_action": rec_action,
                    "limitations": limitation_text,
                    "status": "OPEN",
                }
            )

            if not created:
                rec.current_monthly_cost = None
                rec.confidence = confidence
                rec.priority = priority
                rec.evidence = evidence_text
                rec.recommended_action = rec_action
                rec.limitations = limitation_text
                rec.user = actor_user
                
                new_hash = generate_explanation_hash(rec)
                if rec.ai_explanation_hash != new_hash:
                    rec.ai_explanation_hash = ""
                    rec.ai_explanation_json = None
                rec.save()

            generated_count += 1

    return generated_count
