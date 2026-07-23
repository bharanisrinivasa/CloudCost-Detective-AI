from django.urls import path
from analytics import views

urlpatterns = [
    path("anomalies/", views.AnomalyListView.as_view(), name="anomaly-list"),
    path("anomalies/<int:pk>/", views.AnomalyDetailView.as_view(), name="anomaly-detail"),
    path("anomalies/trigger/", views.TriggerAnomalyDetectionView.as_view(), name="anomaly-trigger"),
    path("anomalies/<int:pk>/status/", views.UpdateAnomalyStatusView.as_view(), name="anomaly-update-status"),
]
