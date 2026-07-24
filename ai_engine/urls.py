from django.urls import path
from ai_engine import views

app_name = "ai_engine"

urlpatterns = [
    path("anomalies/<int:pk>/explain/", views.ExplainAnomalyView.as_view(), name="explain-anomaly"),
    path("waste/<int:pk>/explain/", views.ExplainWasteView.as_view(), name="explain-waste"),
]
