from django.urls import path

from .views import (
    BillingRecordListView,
    BillingUploadDetailView,
    BillingUploadListCreateView,
    upload_billing_view,
    upload_usage_view,
    upload_history_view,
    upload_detail_view,
    upload_delete_view,
)

urlpatterns = [
    # Legacy DRF API routes
    path("uploads/", BillingUploadListCreateView.as_view(), name="billing-upload-list"),
    path("uploads/<int:pk>/", BillingUploadDetailView.as_view(), name="billing-upload-detail"),
    path("uploads/<int:upload_id>/records/", BillingRecordListView.as_view(), name="billing-record-list"),

    # HTML Views
    path("upload/billing/", upload_billing_view, name="upload-billing"),
    path("upload/usage/", upload_usage_view, name="upload-usage"),
    path("history/", upload_history_view, name="upload-history"),
    path("<int:pk>/", upload_detail_view, name="upload-detail"),
    path("<int:pk>/delete/", upload_delete_view, name="upload-delete"),
]

