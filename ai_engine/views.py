import logging
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden
from django.urls import reverse

logger = logging.getLogger(__name__)

from analytics.models import CostAnomaly, WasteFinding
from ai_engine.services.explanation_service import get_or_generate_explanation
from ai_engine.services.provider import (
    LLMMissingAPIKeyError,
    LLMTimeoutError,
    LLMRateLimitError,
    LLMInvalidResponseError,
    LLMException,
)


from accounts.permissions import ProjectPermissionRequiredMixin
from ai_engine.services.explanation_service import get_or_generate_explanation_for_project


class ExplainAnomalyView(ProjectPermissionRequiredMixin, View):
    """
    POST view to synchronously generate or regenerate an AI explanation
    for a specific CostAnomaly instance owned by the active project.
    """
    required_capability = "GENERATE_AI"

    def post(self, request, pk, *args, **kwargs):
        anomaly = get_object_or_404(CostAnomaly, pk=pk)
        from django.core.exceptions import PermissionDenied
        from accounts.models import OrganizationMembership
        if not OrganizationMembership.objects.filter(user=request.user, organization=anomaly.project.organization).exists():
            raise PermissionDenied("You do not have access to this project.")
        if anomaly.project != self.active_project:
            raise PermissionDenied("Object does not belong to the active project.")
            
        force_regenerate = request.POST.get("regenerate", "false").lower() == "true"

        try:
            get_or_generate_explanation_for_project(
                project=self.active_project,
                source=anomaly,
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

        return redirect(reverse("anomaly-detail", kwargs={"pk": pk}))


class ExplainWasteView(ProjectPermissionRequiredMixin, View):
    """
    POST view to synchronously generate or regenerate an AI explanation
    for a specific WasteFinding instance owned by the active project.
    """
    required_capability = "GENERATE_AI"

    def post(self, request, pk, *args, **kwargs):
        finding = get_object_or_404(WasteFinding, pk=pk)
        from django.core.exceptions import PermissionDenied
        from accounts.models import OrganizationMembership
        if not OrganizationMembership.objects.filter(user=request.user, organization=finding.project.organization).exists():
            raise PermissionDenied("You do not have access to this project.")
        if finding.project != self.active_project:
            raise PermissionDenied("Object does not belong to the active project.")
            
        force_regenerate = request.POST.get("regenerate", "false").lower() == "true"

        try:
            get_or_generate_explanation_for_project(
                project=self.active_project,
                source=finding,
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

        return redirect(reverse("waste-detail", kwargs={"pk": pk}))


class ChatIndexView(ProjectPermissionRequiredMixin, View):
    required_capability = "USE_CHAT"

    def get(self, request, *args, **kwargs):
        from ai_engine.services.chat.chat_service import get_chat_sessions_for_project_user
        sessions = get_chat_sessions_for_project_user(self.active_project, request.user)
        active_session = sessions.first()
        if active_session:
            return redirect(reverse("ai_engine:chat-session", kwargs={"session_id": active_session.pk}))
            
        context = {
            "sessions": sessions,
            "active_session": None,
            "messages_list": [],
            "active_project": self.active_project
        }
        return render(request, "ai_engine/chat.html", context)


class ChatNewView(ProjectPermissionRequiredMixin, View):
    required_capability = "USE_CHAT"

    def post(self, request, *args, **kwargs):
        from ai_engine.services.chat.chat_service import get_or_create_chat_session_for_project
        session = get_or_create_chat_session_for_project(self.active_project, request.user)
        return redirect(reverse("ai_engine:chat-session", kwargs={"session_id": session.pk}))


class ChatSessionView(ProjectPermissionRequiredMixin, View):
    required_capability = "USE_CHAT"

    def get(self, request, session_id, *args, **kwargs):
        from ai_engine.services.chat.chat_service import get_chat_sessions_for_project_user, get_or_create_chat_session_for_project
        sessions = get_chat_sessions_for_project_user(self.active_project, request.user)
        active_session = get_or_create_chat_session_for_project(self.active_project, request.user, session_id=session_id)
            
        context = {
            "sessions": sessions,
            "active_session": active_session,
            "messages_list": active_session.messages.all(),
            "active_project": self.active_project
        }
        return render(request, "ai_engine/chat.html", context)


class ChatSendView(ProjectPermissionRequiredMixin, View):
    required_capability = "USE_CHAT"

    def post(self, request, session_id, *args, **kwargs):
        from ai_engine.services.chat.chat_service import get_or_create_chat_session_for_project, send_chat_message_for_project
        active_session = get_or_create_chat_session_for_project(self.active_project, request.user, session_id=session_id)

        message_content = request.POST.get("message", "").strip()
        if message_content:
            try:
                send_chat_message_for_project(self.active_project, request.user, active_session.pk, message_content)
            except Exception as e:
                logger.error("Chat send error: %s", type(e).__name__)
                messages.error(request, "Failed to process chat message.")
        else:
            messages.warning(request, "Cannot send an empty message.")
            
        return redirect(reverse("ai_engine:chat-session", kwargs={"session_id": active_session.pk}))


class ChatDeleteView(ProjectPermissionRequiredMixin, View):
    required_capability = "USE_CHAT"

    def post(self, request, session_id, *args, **kwargs):
        from ai_engine.services.chat.chat_service import delete_chat_session_for_project
        delete_chat_session_for_project(self.active_project, request.user, session_id)
        messages.success(request, "Chat session deleted successfully.")
        return redirect(reverse("ai_engine:chat-index"))

