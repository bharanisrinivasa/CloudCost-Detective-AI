from django.urls import path
from .views import connection_detail_view, test_connection_ajax, trigger_sync_ajax

app_name = "oci_connector"

urlpatterns = [
    path("", connection_detail_view, name="connection-detail"),
    path("test/", test_connection_ajax, name="test-connection"),
    path("sync/", trigger_sync_ajax, name="trigger-sync"),
]
