from django.urls import path
from ai_engine import views

app_name = "ai_engine"

urlpatterns = [
    path("anomalies/<int:pk>/explain/", views.ExplainAnomalyView.as_view(), name="explain-anomaly"),
    path("waste/<int:pk>/explain/", views.ExplainWasteView.as_view(), name="explain-waste"),
    path("chat/", views.ChatIndexView.as_view(), name="chat-index"),
    path("chat/new/", views.ChatNewView.as_view(), name="chat-new"),
    path("chat/<int:session_id>/", views.ChatSessionView.as_view(), name="chat-session"),
    path("chat/<int:session_id>/send/", views.ChatSendView.as_view(), name="chat-send"),
    path("chat/<int:session_id>/delete/", views.ChatDeleteView.as_view(), name="chat-delete"),
]
