import logging
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from ai_engine.models import ChatSession, ChatMessage
from ai_engine.services.chat.intent_schema import QueryPlanValidator
from ai_engine.services.chat.query_planner import plan_chat_query
from ai_engine.services.chat.query_executor import execute_query_plan
from ai_engine.services.chat.response_builder import build_grounded_response

logger = logging.getLogger(__name__)

def get_chat_sessions_for_user(user):
    """Retrieves all chat sessions for a user, ordered by most recently updated."""
    return ChatSession.objects.filter(user=user).order_by("-updated_at")

def get_or_create_chat_session(user, session_id=None) -> ChatSession:
    """Helper to retrieve or create a new ChatSession for isolation check."""
    if session_id:
        return get_object_or_404(ChatSession, pk=session_id, user=user)
    return ChatSession.objects.create(user=user, title="New Chat")

def delete_chat_session(user, session_id) -> None:
    """Deletes a ChatSession after verifying ownership."""
    session = get_object_or_404(ChatSession, pk=session_id, user=user)
    session.delete()

def send_chat_message(user, session_id, message_content) -> ChatMessage:
    """
    Orchestrates the chat message pipeline:
    1. Saves USER message.
    2. Updates session title if it's the first message.
    3. Calls Query Planner (with conversation history context).
    4. Performs plan validation. If invalid, generates an immediate error message response.
    5. Executes plan deterministically via Django ORM.
    6. Calls Grounded Response Builder (or fallbacks).
    7. Stores ASSISTANT message with audit metadata.
    """
    session = get_object_or_404(ChatSession, pk=session_id, user=user)
    message_content = message_content.strip()

    if not message_content:
        raise ValidationError("Message content cannot be empty.")

    with transaction.atomic():
        # 1. Save USER message
        user_message = ChatMessage.objects.create(
            session=session,
            role="USER",
            content=message_content
        )

        # 2. Update session title if first message
        first_msg = session.messages.filter(role="USER").first()
        if first_msg and first_msg.pk == user_message.pk:
            title_text = message_content[:50]
            if len(message_content) > 50:
                title_text += "..."
            session.title = title_text
            session.save()

    # Get history prior to user's current message
    history_qs = session.messages.exclude(pk=user_message.pk)

    # 3. Call Query Planner
    try:
        plan = plan_chat_query(message_content, history_messages=history_qs)
    except Exception as e:
        logger.error("Query planner failed: %s", type(e).__name__)
        # Hard fallback: help plan
        from ai_engine.services.chat.intent_schema import ChatQueryPlan, IntentEnum, TimeRangeSchema, TimeRangeTypeEnum
        plan = ChatQueryPlan(
            intent=IntentEnum.HELP,
            time_range=TimeRangeSchema(type=TimeRangeTypeEnum.LAST_30_DAYS)
        )

    # 4. Perform Application-Level Validation
    try:
        QueryPlanValidator.validate(plan)
        validation_error_msg = None
    except ValidationError as ve:
        logger.warning("Query plan validation failed: %s", str(ve))
        validation_error_msg = str(ve.message) if hasattr(ve, 'message') else str(ve)

    if validation_error_msg:
        # Save validation error ASSISTANT message immediately (skipping query execution & Gemini response)
        with transaction.atomic():
            assistant_message = ChatMessage.objects.create(
                session=session,
                role="ASSISTANT",
                content=validation_error_msg,
                intent=plan.intent.value,
                query_plan=plan.model_dump()
            )
            session.save()  # update updated_at timestamp
        return assistant_message

    # 5. Execute Plan Deterministically via Django ORM
    query_failed = False
    try:
        context = execute_query_plan(user, plan)
    except Exception as e:
        logger.error("Chat query executor failed: %s", type(e).__name__)
        query_failed = True
        context = None

    if query_failed:
        error_msg = "I couldn't retrieve the billing data for that request. Please try again."
        with transaction.atomic():
            assistant_message = ChatMessage.objects.create(
                session=session,
                role="ASSISTANT",
                content=error_msg,
                intent=plan.intent.value,
                query_plan=plan.model_dump(),
                deterministic_context={"error": "Chat query executor failed"}
            )
            session.save()
        return assistant_message

    # 6. Call Grounded Response Builder
    response_content = build_grounded_response(message_content, plan, context)

    # 7. Store ASSISTANT message with audit metadata
    with transaction.atomic():
        assistant_message = ChatMessage.objects.create(
            session=session,
            role="ASSISTANT",
            content=response_content,
            intent=plan.intent.value,
            query_plan=plan.model_dump(),
            deterministic_context=context
        )
        session.save()  # update updated_at timestamp

    return assistant_message
