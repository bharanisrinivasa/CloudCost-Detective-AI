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
