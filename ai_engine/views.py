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


class ChatIndexView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        from ai_engine.services.chat.chat_service import get_chat_sessions_for_user
        sessions = get_chat_sessions_for_user(request.user)
        active_session = sessions.first()
        if active_session:
            return redirect(reverse("ai_engine:chat-session", kwargs={"session_id": active_session.pk}))
            
        context = {
            "sessions": sessions,
            "active_session": None,
            "messages_list": [],
        }
        return render(request, "ai_engine/chat.html", context)


class ChatNewView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        from ai_engine.services.chat.chat_service import get_or_create_chat_session
        session = get_or_create_chat_session(request.user)
        return redirect(reverse("ai_engine:chat-session", kwargs={"session_id": session.pk}))


class ChatSessionView(LoginRequiredMixin, View):
    def get(self, request, session_id, *args, **kwargs):
        from ai_engine.services.chat.chat_service import get_chat_sessions_for_user, get_or_create_chat_session
        sessions = get_chat_sessions_for_user(request.user)
        active_session = get_or_create_chat_session(request.user, session_id=session_id)
            
        context = {
            "sessions": sessions,
            "active_session": active_session,
            "messages_list": active_session.messages.all(),
        }
        return render(request, "ai_engine/chat.html", context)


class ChatSendView(LoginRequiredMixin, View):
    def post(self, request, session_id, *args, **kwargs):
        from ai_engine.services.chat.chat_service import get_or_create_chat_session, send_chat_message
        active_session = get_or_create_chat_session(request.user, session_id=session_id)

        message_content = request.POST.get("message", "").strip()
        if message_content:
            try:
                send_chat_message(request.user, active_session.pk, message_content)
            except Exception as e:
                logger.error("Chat send error: %s", type(e).__name__)
                messages.error(request, "Failed to process chat message.")
        else:
            messages.warning(request, "Cannot send an empty message.")
            
        return redirect(reverse("ai_engine:chat-session", kwargs={"session_id": active_session.pk}))


class ChatDeleteView(LoginRequiredMixin, View):
    def post(self, request, session_id, *args, **kwargs):
        from ai_engine.services.chat.chat_service import delete_chat_session
        delete_chat_session(request.user, session_id)
        messages.success(request, "Chat session deleted successfully.")
        return redirect(reverse("ai_engine:chat-index"))

