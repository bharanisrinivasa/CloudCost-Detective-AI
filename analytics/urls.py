from django.urls import path
from analytics import views

urlpatterns = [
    path("anomalies/", views.AnomalyListView.as_view(), name="anomaly-list"),
    path("anomalies/<int:pk>/", views.AnomalyDetailView.as_view(), name="anomaly-detail"),
    path("anomalies/trigger/", views.TriggerAnomalyDetectionView.as_view(), name="anomaly-trigger"),
    path("anomalies/<int:pk>/status/", views.UpdateAnomalyStatusView.as_view(), name="anomaly-update-status"),
    
    # Waste Detection Routes
    path("waste/", views.WasteListView.as_view(), name="waste-list"),
    path("waste/<int:pk>/", views.WasteDetailView.as_view(), name="waste-detail"),
    path("waste/trigger/", views.TriggerWasteDetectionView.as_view(), name="waste-trigger"),
    path("waste/<int:pk>/status/", views.UpdateWasteStatusView.as_view(), name="waste-update-status"),
]
