import logging
import threading
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.views.decorators.http import require_POST

from accounts.permissions import get_active_project, has_project_permission
from oci_connector.models import (
    OCIConnection,
    OCISyncLog,
    OCIComputeInstance,
    OCIVolume,
    OCIObjectStorageBucket,
    OCIPublicIp,
    OCILoadBalancer,
)
from oci_connector.services.encryption import encrypt_private_key
from oci_connector.services.oci_client import test_oci_connection_stages, sanitize_oci_error, KNOWN_OCI_EXCEPTIONS
from oci_connector.services.sync_service import OCISyncService

logger = logging.getLogger(__name__)


def run_sync_in_thread(connection_id):
    """
    Fallback background execution when Celery is not active.
    """
    def run():
        try:
            connection = OCIConnection.objects.get(pk=connection_id)
            sync_service = OCISyncService(connection)
            sync_service.sync_all()
        except KNOWN_OCI_EXCEPTIONS as e:
            safe_message = sanitize_oci_error(e, operation="thread OCI synchronization")
            logger.error("Background sync thread failed for connection %s: %s", connection_id, safe_message)
        except Exception:
            logger.exception("Background sync thread failed for connection %s due to unexpected internal error", connection_id)
            
    thread = threading.Thread(target=run)
    thread.daemon = True
    thread.start()


@login_required
def connection_detail_view(request):
    """
    Main OCI Connection view page.
    Handles viewing OCI credentials status, sync logs, and forms to create/update.
    """
    active_project = get_active_project(request)
    if not active_project:
        messages.warning(request, "Please select or create a project first.")
        return redirect("accounts:project-list")

    # RBAC capability check: VIEW_OCI_CONNECTION
    if not has_project_permission(request.user, active_project, "VIEW_OCI_CONNECTION"):
        raise PermissionDenied("You do not have permission to view OCI Connection configuration.")

    # Check if a connection exists for the active project
    connection = OCIConnection.objects.filter(project=active_project).first()

    # Permissions checks for buttons
    can_manage = has_project_permission(request.user, active_project, "MANAGE_OCI_CONNECTION")
    can_sync = has_project_permission(request.user, active_project, "RUN_OCI_SYNC")

    if request.method == "POST":
        if not can_manage:
            raise PermissionDenied("You do not have permission to modify OCI Connection configuration.")

        name = request.POST.get("name", "").strip()
        tenancy_ocid = request.POST.get("tenancy_ocid", "").strip()
        user_ocid = request.POST.get("user_ocid", "").strip()
        fingerprint = request.POST.get("fingerprint", "").strip()
        region = request.POST.get("region", "").strip()
        compartment_ocid = request.POST.get("compartment_ocid", "").strip()
        private_key = request.POST.get("private_key", "").strip()

        # Input Validation
        if not name or not tenancy_ocid or not user_ocid or not fingerprint or not region or not compartment_ocid:
            messages.error(request, "All fields are required.")
            return redirect("oci_connector:connection-detail")

        if not connection:
            # Create
            if not private_key:
                messages.error(request, "Private key is required when setting up a new connection.")
                return redirect("oci_connector:connection-detail")
            
            try:
                encrypted_key = encrypt_private_key(private_key)
                connection = OCIConnection.objects.create(
                    project=active_project,
                    name=name,
                    tenancy_ocid=tenancy_ocid,
                    user_ocid=user_ocid,
                    fingerprint=fingerprint,
                    private_key_encrypted=encrypted_key,
                    region=region,
                    compartment_ocid=compartment_ocid,
                )
                messages.success(request, "OCI Connection configured successfully.")
            except KNOWN_OCI_EXCEPTIONS as e:
                safe_message = sanitize_oci_error(e)
                logger.error("OCI error configuring Connection: %s", safe_message)
                messages.error(request, f"Configuration failed: {safe_message}")
            except Exception:
                logger.exception("Error configuring OCI Connection")
                messages.error(request, "Unable to complete the OCI operation. Please try again or review the connection configuration.")
        else:
            # Update
            try:
                connection.name = name
                connection.tenancy_ocid = tenancy_ocid
                connection.user_ocid = user_ocid
                connection.fingerprint = fingerprint
                connection.region = region
                connection.compartment_ocid = compartment_ocid

                # Only overwrite private key if it was edited
                if private_key and private_key != "********":
                    connection.private_key_encrypted = encrypt_private_key(private_key)

                connection.save()
                messages.success(request, "OCI Connection updated successfully.")
            except KNOWN_OCI_EXCEPTIONS as e:
                safe_message = sanitize_oci_error(e)
                logger.error("OCI error updating Connection: %s", safe_message)
                messages.error(request, f"Update failed: {safe_message}")
            except Exception:
                logger.exception("Error updating OCI Connection")
                messages.error(request, "Unable to complete the OCI operation. Please try again or review the connection configuration.")

        return redirect("oci_connector:connection-detail")

    # Fetch inventory counts
    inventory = {
        "instances": OCIComputeInstance.objects.filter(project=active_project, inventory_status="PRESENT").count(),
        "volumes": OCIVolume.objects.filter(project=active_project, inventory_status="PRESENT").count(),
        "buckets": OCIObjectStorageBucket.objects.filter(project=active_project, inventory_status="PRESENT").count(),
        "public_ips": OCIPublicIp.objects.filter(project=active_project, inventory_status="PRESENT").count(),
        "load_balancers": OCILoadBalancer.objects.filter(project=active_project, inventory_status="PRESENT").count(),
    }

    # Fetch recent sync logs
    sync_logs = OCISyncLog.objects.filter(project=active_project).order_by("-started_at")[:10]

    context = {
        "active_project": active_project,
        "connection": connection,
        "inventory": inventory,
        "sync_logs": sync_logs,
        "can_manage": can_manage,
        "can_sync": can_sync,
    }
    return render(request, "oci_connector/connection_detail.html", context)


@login_required
@require_POST
def test_connection_ajax(request):
    """
    AJAX endpoint to test OCI connection in stages.
    """
    active_project = get_active_project(request)
    if not active_project:
        return JsonResponse({"error": "Project not found"}, status=404)

    if not has_project_permission(request.user, active_project, "VIEW_OCI_CONNECTION"):
        return JsonResponse({"error": "Permission denied"}, status=403)

    connection = get_object_or_404(OCIConnection, project=active_project)

    try:
        stages = test_oci_connection_stages(connection)
        success = all("FAILED" not in str(v) for v in stages.values())
        return JsonResponse({"success": success, "stages": stages})
    except KNOWN_OCI_EXCEPTIONS as e:
        safe_message = sanitize_oci_error(e)
        logger.error("OCI error during stages testing: %s", safe_message)
        return JsonResponse({"success": False, "error": "Unable to complete the OCI operation. Please try again or review the connection configuration."})
    except Exception:
        logger.exception("Unexpected error during stages testing")
        return JsonResponse({"success": False, "error": "Unable to complete the OCI operation. Please try again or review the connection configuration."})


@login_required
@require_POST
def trigger_sync_ajax(request):
    """
    AJAX endpoint to manually trigger cost, inventory, and metrics synchronization.
    """
    active_project = get_active_project(request)
    if not active_project:
        return JsonResponse({"error": "Project not found"}, status=404)

    if not has_project_permission(request.user, active_project, "RUN_OCI_SYNC"):
        return JsonResponse({"error": "Permission denied"}, status=403)

    connection = get_object_or_404(OCIConnection, project=active_project)

    # Check for already running sync logs
    active_running = OCISyncLog.objects.filter(connection=connection, status="PROCESSING").exists()
    if active_running:
        return JsonResponse({"error": "A synchronization is already in progress."}, status=400)

    # Try enqueuing with Celery if configured and available
    try:
        from oci_connector.tasks import sync_oci_data_task
        sync_oci_data_task.delay(connection.id)
        return JsonResponse({"success": True, "message": "Synchronization enqueued in background via Celery."})
    except Exception as e:
        logger.warning("Celery task dispatch failed, falling back to background thread.")
        # Fallback to local background thread
        run_sync_in_thread(connection.id)
        return JsonResponse({"success": True, "message": "Synchronization started in a background thread."})
