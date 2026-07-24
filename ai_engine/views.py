from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden
from django.urls import reverse

from analytics.models import CostAnomaly, WasteFinding
from ai_engine.services.explanation_service import get_or_generate_explanation
from ai_engine.services.provider import (
    LLMMissingAPIKeyError,
    LLMTimeoutError,
    LLMRateLimitError,
    LLMInvalidResponseError,
    LLMException,
)


class ExplainAnomalyView(LoginRequiredMixin, View):
    """
    POST view to synchronously generate or regenerate an AI explanation
    for a specific CostAnomaly instance owned by the requesting user.
    """
    def post(self, request, pk, *args, **kwargs):
        anomaly = get_object_or_404(CostAnomaly, pk=pk)

        # Scoping validation
        if anomaly.user != request.user:
            return HttpResponseForbidden("You are not authorized to explain this anomaly.")

        force_regenerate = request.POST.get("regenerate", "false").lower() == "true"

        try:
            get_or_generate_explanation(
                user=request.user,
                source=anomaly,
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

        return redirect(reverse("anomaly-detail", kwargs={"pk": pk}))


class ExplainWasteView(LoginRequiredMixin, View):
    """
    POST view to synchronously generate or regenerate an AI explanation
    for a specific WasteFinding instance owned by the requesting user.
    """
    def post(self, request, pk, *args, **kwargs):
        finding = get_object_or_404(WasteFinding, pk=pk)

        # Scoping validation
        if finding.user != request.user:
            return HttpResponseForbidden("You are not authorized to explain this waste finding.")

        force_regenerate = request.POST.get("regenerate", "false").lower() == "true"

        try:
            get_or_generate_explanation(
                user=request.user,
                source=finding,
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

        return redirect(reverse("waste-detail", kwargs={"pk": pk}))
