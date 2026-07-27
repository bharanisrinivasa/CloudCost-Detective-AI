from django.shortcuts import render, get_object_or_404, redirect
from django.views import View
from django.contrib import messages
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from accounts.permissions import ProjectPermissionRequiredMixin, get_active_project
from analytics.models import CostAnomaly
from analytics.services.anomaly_detector import run_anomaly_detection_for_project

class AnomalyListView(ProjectPermissionRequiredMixin, View):
    template_name = "analytics/anomaly_list.html"
    required_capability = "VIEW_DASHBOARD"
    
    def get(self, request, *args, **kwargs):
        queryset = CostAnomaly.objects.filter(project=self.active_project).order_by('-detected_date', '-detected_at')
        
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
            },
            'active_project': self.active_project
        }
        return render(request, self.template_name, context)

class AnomalyDetailView(ProjectPermissionRequiredMixin, View):
    template_name = "analytics/anomaly_detail.html"
    required_capability = "VIEW_DASHBOARD"
    
    def get(self, request, pk, *args, **kwargs):
        anomaly = get_object_or_404(CostAnomaly, pk=pk)
        from accounts.models import OrganizationMembership
        if not OrganizationMembership.objects.filter(user=request.user, organization=anomaly.project.organization).exists():
            raise PermissionDenied("You do not have access to this project.")
        if anomaly.project != self.active_project:
            raise PermissionDenied("Object does not belong to the active project.")
            
        context = {
            'anomaly': anomaly,
            'status_choices': CostAnomaly.STATUS_CHOICES,
            'active_project': self.active_project
        }
        return render(request, self.template_name, context)

class TriggerAnomalyDetectionView(ProjectPermissionRequiredMixin, View):
    required_capability = "RUN_ANALYTICS"

    def post(self, request, *args, **kwargs):
        # Run synchronous detection scoped to project
        results = run_anomaly_detection_for_project(self.active_project, actor_user=request.user)
        
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

class UpdateAnomalyStatusView(ProjectPermissionRequiredMixin, View):
    required_capability = "UPDATE_ANALYTICS_STATUS"

    def post(self, request, pk, *args, **kwargs):
        anomaly = get_object_or_404(CostAnomaly, pk=pk)
        from accounts.models import OrganizationMembership
        if not OrganizationMembership.objects.filter(user=request.user, organization=anomaly.project.organization).exists():
            raise PermissionDenied("You do not have access to this project.")
        if anomaly.project != self.active_project:
            raise PermissionDenied("Object does not belong to the active project.")
            
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
from analytics.services.waste_detector import run_waste_detection_for_project

class WasteListView(ProjectPermissionRequiredMixin, View):
    template_name = "analytics/waste_list.html"
    required_capability = "VIEW_DASHBOARD"
    
    def get(self, request, *args, **kwargs):
        queryset = WasteFinding.objects.filter(project=self.active_project).order_by('-total_cost', '-detected_at')
        
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
            WasteFinding.objects.filter(project=self.active_project)
            .values_list('service_name', flat=True)
            .distinct()
            .order_by('service_name')
        )
        distinct_regions = list(
            WasteFinding.objects.filter(project=self.active_project)
            .values_list('region', flat=True)
            .distinct()
            .order_by('region')
        )
        
        # Calculate summary savings grouped by currency for open findings
        open_findings = WasteFinding.objects.filter(project=self.active_project, status="OPEN")
        savings_by_currency = (
            open_findings.values('currency')
            .annotate(total_savings=Sum('estimated_monthly_savings'))
            .order_by('currency')
        )
        
        # Deduplicated savings display
        savings_parts = []
        for item in savings_by_currency:
            savings_parts.append(f"{item['total_savings']:.2f} {item['currency']}")
        
        potential_savings_display = ", ".join(savings_parts) if savings_parts else "0.00 USD"
        has_multiple_currencies = len(savings_by_currency) > 1
        
        # Total wasteful resources analyzed
        from billing.models import BillingRecord
        total_analyzed_resources = (
            BillingRecord.objects.filter(upload__project=self.active_project)
            .exclude(resource_id__isnull=True)
            .exclude(resource_id="")
            .values('resource_id')
            .distinct()
            .count()
        )
        if total_analyzed_resources == 0:
            total_analyzed_resources = (
                BillingRecord.objects.filter(upload__project=self.active_project)
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
            },
            'active_project': self.active_project
        }
        return render(request, self.template_name, context)

class WasteDetailView(ProjectPermissionRequiredMixin, View):
    template_name = "analytics/waste_detail.html"
    required_capability = "VIEW_DASHBOARD"
    
    def get(self, request, pk, *args, **kwargs):
        finding = get_object_or_404(WasteFinding, pk=pk)
        from accounts.models import OrganizationMembership
        if not OrganizationMembership.objects.filter(user=request.user, organization=finding.project.organization).exists():
            raise PermissionDenied("You do not have access to this project.")
        if finding.project != self.active_project:
            raise PermissionDenied("Object does not belong to the active project.")
            
        context = {
            'finding': finding,
            'status_choices': WasteFinding.STATUS_CHOICES,
            'active_project': self.active_project
        }
        return render(request, self.template_name, context)

class TriggerWasteDetectionView(ProjectPermissionRequiredMixin, View):
    required_capability = "RUN_ANALYTICS"

    def post(self, request, *args, **kwargs):
        import time
        start_time = time.time()
        
        results = run_waste_detection_for_project(self.active_project, actor_user=request.user)
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

class UpdateWasteStatusView(ProjectPermissionRequiredMixin, View):
    required_capability = "UPDATE_ANALYTICS_STATUS"

    def post(self, request, pk, *args, **kwargs):
        finding = get_object_or_404(WasteFinding, pk=pk)
        from accounts.models import OrganizationMembership
        if not OrganizationMembership.objects.filter(user=request.user, organization=finding.project.organization).exists():
            raise PermissionDenied("You do not have access to this project.")
        if finding.project != self.active_project:
            raise PermissionDenied("Object does not belong to the active project.")
            
        new_status = request.POST.get('status', '').upper()
        valid_statuses = [choice[0] for choice in WasteFinding.STATUS_CHOICES]
        
        if new_status in valid_statuses:
            finding.status = new_status
            finding.save()
            messages.success(request, f"Waste finding status updated to {finding.get_status_display()}.")
        else:
            messages.error(request, "Invalid status state transition.")
            
        return redirect(reverse('waste-detail', kwargs={'pk': pk}))


class RecommendationListView(ProjectPermissionRequiredMixin, View):
    template_name = "analytics/recommendation_list.html"
    required_capability = "VIEW_DASHBOARD"

    def get(self, request, *args, **kwargs):
        from ai_engine.models import Recommendation
        queryset = Recommendation.objects.filter(project=self.active_project).order_by('-detected_at')

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

        # Retrieve summary metrics for all open project recommendations (unfiltered)
        open_recs = Recommendation.objects.filter(project=self.active_project, status="OPEN")
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
            },
            "active_project": self.active_project
        }
        return render(request, self.template_name, context)


class RecommendationDetailView(ProjectPermissionRequiredMixin, View):
    template_name = "analytics/recommendation_detail.html"
    required_capability = "VIEW_DASHBOARD"

    def get(self, request, pk, *args, **kwargs):
        from ai_engine.models import Recommendation
        rec = get_object_or_404(Recommendation, pk=pk, project=self.active_project)

        # Resolve deterministic source object securely (project-scoped)
        source_obj = None
        if rec.source_type == "WASTE_FINDING" and rec.source_id is not None:
            source_obj = WasteFinding.objects.filter(pk=rec.source_id, project=self.active_project).first()
        elif rec.source_type == "COST_ANOMALY" and rec.source_id is not None:
            source_obj = CostAnomaly.objects.filter(pk=rec.source_id, project=self.active_project).first()

        context = {
            "recommendation": rec,
            "source_object": source_obj,
            "status_choices": Recommendation.STATUS_CHOICES,
            "active_project": self.active_project
        }
        return render(request, self.template_name, context)

    def post(self, request, pk, *args, **kwargs):
        """POST view to trigger AI Explanation generation on-demand."""
        from ai_engine.models import Recommendation
        from ai_engine.services.explanation_service import get_or_generate_recommendation_explanation_for_project
        from ai_engine.services.provider import (
            LLMMissingAPIKeyError,
            LLMTimeoutError,
            LLMRateLimitError,
            LLMInvalidResponseError,
            LLMException,
        )

        rec = get_object_or_404(Recommendation, pk=pk, project=self.active_project)
        force_regenerate = request.POST.get("regenerate", "false").lower() == "true"

        try:
            get_or_generate_recommendation_explanation_for_project(
                project=self.active_project,
                rec=rec,
                force_regenerate=force_regenerate,
                actor_user=request.user
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


class TriggerRecommendationsView(ProjectPermissionRequiredMixin, View):
    required_capability = "RUN_ANALYTICS"

    def post(self, request, *args, **kwargs):
        from analytics.services.recommendation_engine import run_recommendation_engine_for_project
        import time
        start_time = time.time()
        
        count = run_recommendation_engine_for_project(self.active_project, actor_user=request.user)
        duration = time.time() - start_time
        
        messages.success(
            request,
            f"Recommendation analysis complete. "
            f"Active recommendations updated/created: {count}. "
            f"Execution time: {duration:.2f} seconds."
        )
        return redirect(reverse("recommendation-list"))


class UpdateRecommendationStatusView(ProjectPermissionRequiredMixin, View):
    required_capability = "UPDATE_ANALYTICS_STATUS"

    def post(self, request, pk, *args, **kwargs):
        from ai_engine.models import Recommendation
        rec = get_object_or_404(Recommendation, pk=pk, project=self.active_project)
        
        new_status = request.POST.get("status", "").upper()
        valid_statuses = [choice[0] for choice in Recommendation.STATUS_CHOICES]
        
        if new_status in valid_statuses:
            rec.status = new_status
            rec.save()
            messages.success(request, f"Recommendation status updated to {rec.get_status_display()}.")
        else:
            messages.error(request, "Invalid status state transition.")
            
        return redirect(reverse("recommendation-detail", kwargs={"pk": pk}))


class ForecastView(ProjectPermissionRequiredMixin, View):
    template_name = "analytics/forecast.html"
    required_capability = "VIEW_DASHBOARD"

    def get(self, request, *args, **kwargs):
        import datetime
        from analytics.services.cost_forecaster import get_forecast_for_project

        forecast_results = get_forecast_for_project(self.active_project)

        def format_month_label(month_str):
            yr, mn = map(int, month_str.split('-'))
            dt = datetime.date(yr, mn, 1)
            return dt.strftime("%b %Y")

        has_multiple_currencies = len(forecast_results) > 1
        has_future_records_overall = any(res["has_future_records"] for res in forecast_results.values())

        chart_data = {}
        for curr, res in forecast_results.items():
            if not res["forecast_available"]:
                continue

            labels = []
            historical_data = []
            forecast_data = []
            lower_bounds = []
            upper_bounds = []

            for hm in res["historical_months"]:
                labels.append(format_month_label(hm["month"]))
                historical_data.append(float(hm["cost"]))
                forecast_data.append(None)
                lower_bounds.append(None)
                upper_bounds.append(None)

            if res["current_month_mtd"]:
                mtd = res["current_month_mtd"]
                labels.append(format_month_label(mtd["month"]))
                historical_data.append(float(mtd["cost"]))
                forecast_data.append(float(mtd["cost"]))
                lower_bounds.append(float(mtd["cost"]))
                upper_bounds.append(float(mtd["cost"]))
            else:
                if historical_data:
                    forecast_data[-1] = historical_data[-1]
                    if res["forecast_months"] and res["forecast_months"][0]["lower_bound"] is not None:
                        lower_bounds[-1] = historical_data[-1]
                        upper_bounds[-1] = historical_data[-1]

            for fm in res["forecast_months"]:
                labels.append(format_month_label(fm["month"]))
                historical_data.append(None)
                forecast_data.append(float(fm["predicted_cost"]))
                if fm["lower_bound"] is not None:
                    lower_bounds.append(float(fm["lower_bound"]))
                    upper_bounds.append(float(fm["upper_bound"]))
                else:
                    lower_bounds.append(None)
                    upper_bounds.append(None)

            chart_data[curr] = {
                "labels": labels,
                "historical_data": historical_data,
                "forecast_data": forecast_data,
                "lower_bounds": lower_bounds,
                "upper_bounds": upper_bounds
            }

        context = {
            "forecast_results": forecast_results,
            "chart_data": chart_data,
            "has_multiple_currencies": has_multiple_currencies,
            "has_future_records_overall": has_future_records_overall,
            "active_project": self.active_project
        }
        return render(request, self.template_name, context)


class CostSimulatorView(ProjectPermissionRequiredMixin, View):
    template_name = "analytics/simulator.html"
    required_capability = "USE_SIMULATOR"

    def get_common_context(self, request):
        from billing.models import BillingRecord
        from ai_engine.models import Recommendation

        # Distinct project services
        distinct_services = list(BillingRecord.objects.filter(
            upload__project=self.active_project
        ).values_list("service", flat=True).distinct())
        distinct_services = sorted(list(set([s.strip() for s in distinct_services if s and s.strip()])))
        if not distinct_services:
            distinct_services = ["Compute", "Database", "Storage"]

        # Project's open recommendations with non-null savings
        recommendations = Recommendation.objects.filter(
            project=self.active_project,
            status="OPEN"
        ).exclude(estimated_monthly_savings__isnull=True)

        # Distinct project currencies
        distinct_currencies = list(BillingRecord.objects.filter(
            upload__project=self.active_project
        ).values_list("currency", flat=True).distinct())
        distinct_currencies = sorted(list(set([c.strip() for c in distinct_currencies if c and c.strip()])))
        if not distinct_currencies:
            distinct_currencies = ["USD"]

        return {
            "available_services": distinct_services,
            "available_currencies": distinct_currencies,
            "open_recommendations": recommendations,
        }

    def get(self, request, *args, **kwargs):
        context = self.get_common_context(request)
        context.update({
            "period": "CURRENT_MONTH",
            "start_date": "",
            "end_date": "",
            "actions": [],
            "simulation_result": None,
            "chart_data_json": None,
            "active_project": self.active_project
        })
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        from analytics.services.cost_simulator import run_cost_simulation_for_project
        from django.contrib import messages
        import json

        period = request.POST.get("period", "CURRENT_MONTH")
        start_date_str = request.POST.get("start_date")
        end_date_str = request.POST.get("end_date")

        action_types = request.POST.getlist("action_type[]")
        services = request.POST.getlist("service[]")
        currencies = request.POST.getlist("currency[]")
        values = request.POST.getlist("value[]")
        recommendation_ids = request.POST.getlist("recommendation_id[]")

        actions = []
        for i in range(len(action_types)):
            act_type = action_types[i]
            if act_type == "RECOMMENDATION_SAVINGS":
                rec_id = recommendation_ids[i] if i < len(recommendation_ids) else None
                actions.append({
                    "action_type": act_type,
                    "recommendation_id": int(rec_id) if rec_id else None
                })
            else:
                svc = services[i] if i < len(services) else ""
                curr = currencies[i] if i < len(currencies) else ""
                val_str = values[i] if i < len(values) else "0"
                actions.append({
                    "action_type": act_type,
                    "service": svc,
                    "currency": curr,
                    "value": val_str
                })

        context = self.get_common_context(request)
        context.update({
            "period": period,
            "start_date": start_date_str,
            "end_date": end_date_str,
            "actions": actions,
            "simulation_result": None,
            "chart_data_json": None,
            "active_project": self.active_project
        })

        try:
            simulation_result = run_cost_simulation_for_project(
                project=self.active_project,
                period=period,
                start_date_str=start_date_str,
                end_date_str=end_date_str,
                actions=actions,
                actor_user=request.user
            )
            
            # Serialize chart data using float format specifically for Chart.js presentation
            chart_data = {}
            for curr, res in simulation_result["currency_results"].items():
                chart_data[curr] = {
                    "currency": curr,
                    "baseline": float(res["baseline_cost"]),
                    "simulated": float(res["simulated_cost"]),
                    "difference": float(res["absolute_change"]),
                    "percentage": float(res["percentage_change"]) if res["percentage_change"] is not None else 0.0
                }
            
            context.update({
                "simulation_result": simulation_result,
                "chart_data_json": json.dumps(chart_data)
            })
            messages.success(request, "Cost simulation completed successfully.")
        except ValueError as e:
            messages.error(request, f"Simulation Error: {str(e)}")
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.exception("Unexpected error in CostSimulatorView: %s", e)
            messages.error(
                request,
                "Unable to run the simulation. Please verify the simulation settings and try again."
            )

        return render(request, self.template_name, context)


class ExecutiveReportView(ProjectPermissionRequiredMixin, View):
    template_name = "analytics/report_builder.html"
    required_capability = "VIEW_REPORTS"

    def get(self, request, *args, **kwargs):
        context = {
            "period": "CURRENT_MONTH",
            "start_date": "",
            "end_date": "",
            "enabled_sections": ["cost_breakdown", "anomalies", "waste", "recommendations", "forecast"],
            "active_project": self.active_project
        }
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        from analytics.services.report_service import collect_report_data_for_project
        from analytics.services.pdf_report_generator import generate_pdf_report
        from django.contrib import messages
        from django.http import FileResponse
        import io

        period = request.POST.get("period", "CURRENT_MONTH")
        start_date_str = request.POST.get("start_date")
        end_date_str = request.POST.get("end_date")

        # Validate sections against strict server-side allowlist
        ALLOWED_SECTIONS = {"cost_breakdown", "anomalies", "waste", "recommendations", "forecast"}
        sections_input = request.POST.getlist("sections[]")
        enabled_sections = [s for s in sections_input if s in ALLOWED_SECTIONS]

        try:
            report_data = collect_report_data_for_project(
                project=self.active_project,
                period=period,
                start_date_str=start_date_str,
                end_date_str=end_date_str,
                enabled_sections=enabled_sections
            )
            
            pdf_bytes = generate_pdf_report(report_data)
            
            response = FileResponse(io.BytesIO(pdf_bytes), content_type="application/pdf")
            filename = f"cloudcost-executive-report-{report_data['start_date']}-to-{report_data['end_date']}.pdf"
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response
            
        except ValueError as e:
            messages.error(request, str(e))
            context = {
                "period": period,
                "start_date": start_date_str,
                "end_date": end_date_str,
                "enabled_sections": enabled_sections,
                "active_project": self.active_project
            }
            return render(request, self.template_name, context)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.exception("Unexpected error in ExecutiveReportView: %s", e)
            messages.error(
                request,
                "Unable to generate the report. Please verify the report settings and try again."
            )
            context = {
                "period": period,
                "start_date": start_date_str,
                "end_date": end_date_str,
                "enabled_sections": enabled_sections,
                "active_project": self.active_project
            }
            return render(request, self.template_name, context)




