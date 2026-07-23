from django.shortcuts import render, get_object_or_404, redirect
from django.views import View
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from analytics.models import CostAnomaly
from analytics.services.anomaly_detector import run_anomaly_detection_for_user

class AnomalyListView(LoginRequiredMixin, View):
    template_name = "analytics/anomaly_list.html"
    
    def get(self, request, *args, **kwargs):
        user = request.user
        queryset = CostAnomaly.objects.filter(user=user).order_by('-detected_date', '-detected_at')
        
        # Get active filters from GET params
        severity = request.GET.get('severity', '')
        anomaly_type = request.GET.get('anomaly_type', '')
        status = request.GET.get('status', '')
        start_date = request.GET.get('start_date', '')
        end_date = request.GET.get('end_date', '')
        
        # Apply filters
        if severity:
            queryset = queryset.filter(severity=severity)
        if anomaly_type:
            queryset = queryset.filter(anomaly_type=anomaly_type)
        if status:
            queryset = queryset.filter(status=status)
        if start_date:
            queryset = queryset.filter(detected_date__gte=start_date)
        if end_date:
            queryset = queryset.filter(detected_date__lte=end_date)
            
        context = {
            'anomalies': queryset,
            'severity_choices': CostAnomaly.SEVERITY_CHOICES,
            'type_choices': CostAnomaly.ANOMALY_TYPE_CHOICES,
            'status_choices': CostAnomaly.STATUS_CHOICES,
            'active_filters': {
                'severity': severity,
                'anomaly_type': anomaly_type,
                'status': status,
                'start_date': start_date,
                'end_date': end_date,
            }
        }
        return render(request, self.template_name, context)

class AnomalyDetailView(LoginRequiredMixin, View):
    template_name = "analytics/anomaly_detail.html"
    
    def get(self, request, pk, *args, **kwargs):
        # Isolation: guarantee scoping to requesting user
        anomaly = get_object_or_404(CostAnomaly, pk=pk)
        if anomaly.user != request.user:
            return HttpResponseForbidden("You are not authorized to view this anomaly.")
            
        context = {
            'anomaly': anomaly,
            'status_choices': CostAnomaly.STATUS_CHOICES,
        }
        return render(request, self.template_name, context)

class TriggerAnomalyDetectionView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        user = request.user
        start_time = timezone.now()
        
        # Run synchronous detection
        results = run_anomaly_detection_for_user(user)
        
        # Add notification messages
        if results.get('message'):
            messages.warning(request, results['message'])
        else:
            total_detected = results['created'] + results['updated'] + results['skipped']
            msg = (
                f"Anomaly detection complete. "
                f"Anomalies detected: {total_detected}. "
                f"New: {results['created']}. "
                f"Updated: {results['updated']}. "
                f"Skipped/Unchanged: {results['skipped']}."
            )
            messages.success(request, msg)
            
        return redirect(reverse('anomaly-list'))

class UpdateAnomalyStatusView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        anomaly = get_object_or_404(CostAnomaly, pk=pk)
        if anomaly.user != request.user:
            return HttpResponseForbidden("You are not authorized to update this anomaly.")
            
        new_status = request.POST.get('status', '').upper()
        valid_statuses = [choice[0] for choice in CostAnomaly.STATUS_CHOICES]
        
        if new_status in valid_statuses:
            anomaly.status = new_status
            anomaly.save()
            messages.success(request, f"Anomaly status updated to {anomaly.get_status_display()}.")
        else:
            messages.error(request, "Invalid status state transition.")
            
        return redirect(reverse('anomaly-detail', kwargs={'pk': pk}))


# --- MODULE 6: WASTE DETECTION VIEWS ---
from django.db.models import Sum
from analytics.models import WasteFinding
from analytics.services.waste_detector import run_waste_detection_for_user

class WasteListView(LoginRequiredMixin, View):
    template_name = "analytics/waste_list.html"
    
    def get(self, request, *args, **kwargs):
        user = request.user
        queryset = WasteFinding.objects.filter(user=user).order_by('-total_cost', '-detected_at')
        
        # Get active filters from GET params
        waste_type = request.GET.get('waste_type', '')
        confidence = request.GET.get('confidence', '')
        status = request.GET.get('status', '')
        service = request.GET.get('service_name', '')
        region = request.GET.get('region', '')
        
        # Apply filters
        if waste_type:
            queryset = queryset.filter(waste_type=waste_type)
        if confidence:
            queryset = queryset.filter(confidence=confidence)
        if status:
            queryset = queryset.filter(status=status)
        if service:
            queryset = queryset.filter(service_name=service)
        if region:
            queryset = queryset.filter(region=region)
            
        # Get distinct services and regions for filters
        distinct_services = list(
            WasteFinding.objects.filter(user=user)
            .values_list('service_name', flat=True)
            .distinct()
            .order_by('service_name')
        )
        distinct_regions = list(
            WasteFinding.objects.filter(user=user)
            .values_list('region', flat=True)
            .distinct()
            .order_by('region')
        )
        
        # Calculate summary savings grouped by currency for open findings
        open_findings = WasteFinding.objects.filter(user=user, status="OPEN")
        savings_by_currency = (
            open_findings.values('currency')
            .annotate(total_savings=Sum('estimated_monthly_savings'))
            .order_by('currency')
        )
        
        savings_parts = []
        for item in savings_by_currency:
            savings_parts.append(f"{item['total_savings']:.2f} {item['currency']}")
        
        potential_savings_display = ", ".join(savings_parts) if savings_parts else "0.00 USD"
        has_multiple_currencies = len(savings_by_currency) > 1
        
        # Total wasteful resources analyzed
        from billing.models import BillingRecord
        total_analyzed_resources = (
            BillingRecord.objects.filter(upload__uploaded_by=user)
            .exclude(resource_id__isnull=True)
            .exclude(resource_id="")
            .values('resource_id')
            .distinct()
            .count()
        )
        if total_analyzed_resources == 0:
            total_analyzed_resources = (
                BillingRecord.objects.filter(upload__uploaded_by=user)
                .exclude(resource_name__isnull=True)
                .exclude(resource_name="")
                .values('resource_name')
                .distinct()
                .count()
            )
            
        context = {
            'findings': queryset,
            'waste_type_choices': [
                ('PERSISTENT_LOW_COST_RESOURCE', 'Persistent Low-Cost Resource'),
                ('DORMANT_COST_PATTERN', 'Dormant Cost Pattern'),
                ('STALE_RESOURCE_COST', 'Stale Resource Cost'),
                ('POSSIBLE_UNUSED_STORAGE', 'Possible Unused Storage'),
            ],
            'confidence_choices': WasteFinding.CONFIDENCE_CHOICES,
            'status_choices': WasteFinding.STATUS_CHOICES,
            'available_services': distinct_services,
            'available_regions': distinct_regions,
            'potential_savings_display': potential_savings_display,
            'has_multiple_currencies': has_multiple_currencies,
            'total_analyzed_resources': total_analyzed_resources,
            'active_filters': {
                'waste_type': waste_type,
                'confidence': confidence,
                'status': status,
                'service_name': service,
                'region': region,
            }
        }
        return render(request, self.template_name, context)

class WasteDetailView(LoginRequiredMixin, View):
    template_name = "analytics/waste_detail.html"
    
    def get(self, request, pk, *args, **kwargs):
        finding = get_object_or_404(WasteFinding, pk=pk)
        if finding.user != request.user:
            return HttpResponseForbidden("You are not authorized to view this waste finding.")
            
        context = {
            'finding': finding,
            'status_choices': WasteFinding.STATUS_CHOICES,
        }
        return render(request, self.template_name, context)

class TriggerWasteDetectionView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        user = request.user
        import time
        start_time = time.time()
        
        results = run_waste_detection_for_user(user)
        duration = time.time() - start_time
        
        total_found = results['created'] + results['updated']
        savings_parts = []
        for cur, val in results['potential_savings'].items():
            savings_parts.append(f"{val:.2f} {cur}")
        potential_savings_display = ", ".join(savings_parts) if savings_parts else "0.00 USD"
        
        msg = (
            f"Waste detection complete. "
            f"Resources analyzed: {results['analyzed']}. "
            f"New findings: {results['created']}. "
            f"Updated findings: {results['updated']}. "
            f"Potential monthly savings: {potential_savings_display}. "
            f"Detection time: {duration:.2f} seconds."
        )
        messages.success(request, msg)
        return redirect(reverse('waste-list'))

class UpdateWasteStatusView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        finding = get_object_or_404(WasteFinding, pk=pk)
        if finding.user != request.user:
            return HttpResponseForbidden("You are not authorized to update this waste finding.")
            
        new_status = request.POST.get('status', '').upper()
        valid_statuses = [choice[0] for choice in WasteFinding.STATUS_CHOICES]
        
        if new_status in valid_statuses:
            finding.status = new_status
            finding.save()
            messages.success(request, f"Waste finding status updated to {finding.get_status_display()}.")
        else:
            messages.error(request, "Invalid status state transition.")
            
        return redirect(reverse('waste-detail', kwargs={'pk': pk}))
