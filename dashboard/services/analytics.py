import datetime
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from billing.models import BillingRecord, BillingUpload

def get_dashboard_metrics(user, filters):
    """
    Backward compatibility wrapper for user-based dashboard metrics calculation.
    """
    from accounts.models import Project
    project = Project.objects.filter(organization__memberships__user=user).first()
    return get_dashboard_metrics_for_project(project, filters)

def get_dashboard_metrics_for_project(project, filters):
    """
    Calculate cost metrics from the database for the authenticated project,
    applying optional date range, service, and region filters.
    """
    if not project:
        return {
            'has_any_data': False,
            'total_uploads': 0,
            'available_services': [],
            'available_regions': [],
            'total_cost': 0.00,
            'total_resources': 0,
            'currency': 'USD',
            'has_multiple_currencies': False,
            'monthly_trend': []
        }
    # 1. Base querysets for project isolation
    base_records = BillingRecord.objects.filter(upload__project=project)
    total_uploads = BillingUpload.objects.filter(project=project).count()
    has_any_data = base_records.exists()
    
    # 2. Available filter options from UNFILTERED base records (user-specific)
    # Exclude empty/null values to avoid confusing filter dropdown options.
    available_services = list(
        base_records.exclude(service__isnull=True)
        .exclude(service="")
        .values_list('service', flat=True)
        .distinct()
        .order_by('service')
    )
    available_services = [s for s in available_services if s.strip() != ""]

    available_regions = list(
        base_records.exclude(region__isnull=True)
        .exclude(region="")
        .values_list('region', flat=True)
        .distinct()
        .order_by('region')
    )
    available_regions = [r for r in available_regions if r.strip() != ""]
    
    # 3. Parse and Validate Date Range Filters
    start_date = filters.get('start_date')
    end_date = filters.get('end_date')
    
    valid_start = None
    valid_end = None
    date_warning = None
    
    if start_date:
        try:
            valid_start = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            date_warning = "Invalid date format. Please select a valid date."
            
    if end_date:
        try:
            valid_end = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            date_warning = "Invalid date format. Please select a valid date."
            
    if not date_warning and valid_start and valid_end and valid_start > valid_end:
        date_warning = "Start date cannot be later than end date."
        # Date filter is bypassed
        valid_start = None
        valid_end = None
        
    # Apply filters to form the separate filtered records queryset
    records = base_records
    
    if valid_start:
        records = records.filter(usage_start__date__gte=valid_start)
    if valid_end:
        records = records.filter(usage_start__date__lte=valid_end)
        
    selected_service = filters.get('service')
    selected_region = filters.get('region')
    
    if selected_service:
        records = records.filter(service=selected_service)
    if selected_region:
        records = records.filter(region=selected_region)
        
    # 4. Currency Detection (from the filtered records)
    distinct_currencies = list(
        records.exclude(currency__isnull=True)
        .exclude(currency="")
        .values_list('currency', flat=True)
        .distinct()
    )
    
    has_multiple_currencies = False
    if len(distinct_currencies) == 1:
        currency = distinct_currencies[0]
    elif len(distinct_currencies) > 1:
        currency = "MULTI"
        has_multiple_currencies = True
    else:
        currency = "USD"  # Fallback default
        
    # 5. Perform aggregates on the filtered queryset
    total_cost = records.aggregate(total=Sum('cost'))['total'] or 0.00
    
    # Unique resources count (excluding null/empty IDs)
    total_resources = (
        records.exclude(resource_id__isnull=True)
        .exclude(resource_id="")
        .values('resource_id')
        .distinct()
        .count()
    )
    
    # Monthly Cost Trend (aggregated via TruncMonth on usage_start)
    monthly_trend = (
        records.filter(usage_start__isnull=False)
        .annotate(month=TruncMonth('usage_start'))
        .values('month')
        .annotate(total=Sum('cost'))
        .order_by('month')
    )
    
    monthly_labels = []
    monthly_data = []
    for item in monthly_trend:
        dt = item['month']
        label = dt.strftime('%b %Y')
        monthly_labels.append(label)
        monthly_data.append(float(item['total'] or 0.0))
        
    # Service-wise Cost Breakdown with blank/null handling ("Unknown Service")
    raw_service_costs = list(
        records.values('service')
        .annotate(total=Sum('cost'))
    )
    service_costs_map = {}
    for item in raw_service_costs:
        svc = (item['service'] or '').strip()
        if not svc:
            svc = "Unknown Service"
        service_costs_map[svc] = service_costs_map.get(svc, 0.0) + float(item['total'] or 0.0)
        
    service_costs = [{'service': k, 'total': v} for k, v in service_costs_map.items()]
    service_costs.sort(key=lambda x: x['total'], reverse=True)
    
    service_labels = [item['service'] for item in service_costs]
    service_data = [item['total'] for item in service_costs]
    
    # Top 5 Expensive Resources (aggregate by resource_id/resource_name or fallback grouping key)
    raw_top_resources = list(
        records.values('resource_id', 'resource_name', 'service', 'region')
        .annotate(total=Sum('cost'))
    )
    resource_map = {}
    for item in raw_top_resources:
        r_id = (item['resource_id'] or '').strip()
        r_name = (item['resource_name'] or '').strip()
        svc = (item['service'] or '').strip()
        reg = (item['region'] or '').strip()
        cost_val = float(item['total'] or 0.0)
        
        if r_id:
            group_key = f"id:{r_id}"
        elif r_name:
            group_key = f"name:{r_name}"
        else:
            group_key = "unknown"
            
        if group_key not in resource_map:
            resource_map[group_key] = {
                'resource_id': r_id,
                'resource_name': r_name,
                'service': svc,
                'region': reg,
                'total': 0.0
            }
        if r_name and not resource_map[group_key]['resource_name']:
            resource_map[group_key]['resource_name'] = r_name
        if svc and not resource_map[group_key]['service']:
            resource_map[group_key]['service'] = svc
        if reg and not resource_map[group_key]['region']:
            resource_map[group_key]['region'] = reg
            
        resource_map[group_key]['total'] += cost_val
        
    top_resources = list(resource_map.values())
    top_resources.sort(key=lambda x: x['total'], reverse=True)
    top_resources = top_resources[:5]
    
    for item in top_resources:
        # Display fallback priority: (1) name, (2) id, (3) "Unknown Resource"
        item['display_name'] = item['resource_name'] or item['resource_id'] or "Unknown Resource"
        item['service'] = item['service'] or "Unknown Service"
        item['region'] = item['region'] or "Unknown Region"
        
    # Top 5 Regions with percentage of filtered overall cost & empty/null mapping
    raw_region_costs = list(
        records.values('region')
        .annotate(total=Sum('cost'))
    )
    region_costs_map = {}
    for item in raw_region_costs:
        reg = (item['region'] or '').strip()
        if not reg:
            reg = "Unknown Region"
        region_costs_map[reg] = region_costs_map.get(reg, 0.0) + float(item['total'] or 0.0)
        
    filtered_total = float(total_cost)
    full_regions = []
    for k, v in region_costs_map.items():
        percentage = (v / filtered_total * 100) if filtered_total > 0 else 0.0
        full_regions.append({
            'region': k,
            'total': v,
            'percentage': round(percentage, 2)
        })
    full_regions.sort(key=lambda x: x['total'], reverse=True)
    top_regions = full_regions[:5]

    return {
        'total_cost': float(total_cost),
        'currency': currency,
        'has_multiple_currencies': has_multiple_currencies,
        'total_uploads': total_uploads,
        'total_resources': total_resources,
        'has_any_data': has_any_data,
        'available_services': available_services,
        'available_regions': available_regions,
        'monthly_labels': monthly_labels,
        'monthly_data': monthly_data,
        'service_costs': service_costs,
        'service_labels': service_labels,
        'service_data': service_data,
        'top_resources': top_resources,
        'top_regions': top_regions,
        'full_regions': full_regions,
        'date_warning': date_warning,
        'active_filters': {
            'start_date': start_date or '',
            'end_date': end_date or '',
            'service': selected_service or '',
            'region': selected_region or '',
        }
    }
