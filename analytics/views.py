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
            'waste_type_choices': WasteFinding.WASTE_TYPE_CHOICES,
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


class RecommendationListView(LoginRequiredMixin, View):
    template_name = "analytics/recommendation_list.html"

    def get(self, request, *args, **kwargs):
        from ai_engine.models import Recommendation
        user = request.user
        queryset = Recommendation.objects.filter(user=user).order_by('-detected_at')

        # Get filter params
        rec_type = request.GET.get("recommendation_type", "")
        priority = request.GET.get("priority", "")
        confidence = request.GET.get("confidence", "")
        status = request.GET.get("status", "")
        service = request.GET.get("service", "")
        region = request.GET.get("region", "")

        if rec_type:
            queryset = queryset.filter(recommendation_type=rec_type)
        if priority:
            queryset = queryset.filter(priority=priority)
        if confidence:
            queryset = queryset.filter(confidence=confidence)
        if status:
            queryset = queryset.filter(status=status)
        if service:
            queryset = queryset.filter(service_name__iexact=service)
        if region:
            queryset = queryset.filter(region__iexact=region)

        # Retrieve summary metrics for all open user recommendations (unfiltered)
        open_recs = Recommendation.objects.filter(user=user, status="OPEN")
        open_count = open_recs.count()
        high_priority_count = open_recs.filter(priority__in=["HIGH", "CRITICAL"]).count()

        # Deduplicate potential savings by currency
        from decimal import Decimal
        savings_map = {}
        seen_waste_ids = set()
        for rec in open_recs:
            if rec.estimated_monthly_savings is None:
                continue
            curr = rec.currency or "USD"
            if rec.savings_source == "WASTE_FINDING" and rec.source_id is not None:
                if rec.source_id in seen_waste_ids:
                    continue
                seen_waste_ids.add(rec.source_id)
            if curr not in savings_map:
                savings_map[curr] = Decimal("0.00")
            savings_map[curr] += rec.estimated_monthly_savings

        savings_parts = [f"{val:.2f} {cur}" for cur, val in savings_map.items()]
        potential_savings_display = ", ".join(savings_parts) if savings_parts else "0.00 USD"

        context = {
            "recommendations": queryset,
            "rec_type_choices": Recommendation.RECOMMENDATION_TYPE_CHOICES,
            "priority_choices": Recommendation.PRIORITY_CHOICES,
            "confidence_choices": Recommendation.CONFIDENCE_CHOICES,
            "status_choices": Recommendation.STATUS_CHOICES,
            "open_count": open_count,
            "high_priority_count": high_priority_count,
            "potential_savings_display": potential_savings_display,
            "active_filters": {
                "recommendation_type": rec_type,
                "priority": priority,
                "confidence": confidence,
                "status": status,
                "service": service,
                "region": region,
            }
        }
        return render(request, self.template_name, context)


class RecommendationDetailView(LoginRequiredMixin, View):
    template_name = "analytics/recommendation_detail.html"

    def get(self, request, pk, *args, **kwargs):
        from ai_engine.models import Recommendation
        rec = get_object_or_404(Recommendation, pk=pk, user=request.user)

        # Resolve deterministic source object securely (user-scoped)
        source_obj = None
        if rec.source_type == "WASTE_FINDING" and rec.source_id is not None:
            source_obj = WasteFinding.objects.filter(pk=rec.source_id, user=request.user).first()
        elif rec.source_type == "COST_ANOMALY" and rec.source_id is not None:
            source_obj = CostAnomaly.objects.filter(pk=rec.source_id, user=request.user).first()

        context = {
            "recommendation": rec,
            "source_object": source_obj,
            "status_choices": Recommendation.STATUS_CHOICES,
        }
        return render(request, self.template_name, context)

    def post(self, request, pk, *args, **kwargs):
        """POST view to trigger AI Explanation generation on-demand."""
        from ai_engine.models import Recommendation
        from ai_engine.services.explanation_service import get_or_generate_recommendation_explanation
        from ai_engine.services.provider import (
            LLMMissingAPIKeyError,
            LLMTimeoutError,
            LLMRateLimitError,
            LLMInvalidResponseError,
            LLMException,
        )

        rec = get_object_or_404(Recommendation, pk=pk, user=request.user)
        force_regenerate = request.POST.get("regenerate", "false").lower() == "true"

        try:
            get_or_generate_recommendation_explanation(
                user=request.user,
                rec=rec,
                force_regenerate=force_regenerate
            )
            messages.success(request, "AI explanation generated successfully.")
        except LLMMissingAPIKeyError:
            messages.error(request, "AI service is not configured.")
        except LLMTimeoutError:
            messages.error(request, "AI explanation generation timed out. Please try again.")
        except LLMRateLimitError:
            messages.error(request, "AI explanation service is temporarily busy. Please try again later.")
        except LLMInvalidResponseError:
            messages.error(request, "The AI provider returned an invalid explanation.")
        except (LLMException, Exception):
            messages.error(request, "AI explanation generation is temporarily unavailable.")

        return redirect(reverse("recommendation-detail", kwargs={"pk": pk}))


class TriggerRecommendationsView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        from analytics.services.recommendation_engine import run_recommendation_engine
        import time
        start_time = time.time()
        
        count = run_recommendation_engine(request.user)
        duration = time.time() - start_time
        
        messages.success(
            request,
            f"Recommendation analysis complete. "
            f"Active recommendations updated/created: {count}. "
            f"Execution time: {duration:.2f} seconds."
        )
        return redirect(reverse("recommendation-list"))


class UpdateRecommendationStatusView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        from ai_engine.models import Recommendation
        rec = get_object_or_404(Recommendation, pk=pk, user=request.user)
        
        new_status = request.POST.get("status", "").upper()
        valid_statuses = [choice[0] for choice in Recommendation.STATUS_CHOICES]
        
        if new_status in valid_statuses:
            rec.status = new_status
            rec.save()
            messages.success(request, f"Recommendation status updated to {rec.get_status_display()}.")
        else:
            messages.error(request, "Invalid status state transition.")
            
        return redirect(reverse("recommendation-detail", kwargs={"pk": pk}))

