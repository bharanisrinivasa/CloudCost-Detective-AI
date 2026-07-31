import logging
from celery import shared_task
from oci_connector.models import OCIConnection
from oci_connector.services.sync_service import OCISyncService
from oci_connector.services.oci_client import KNOWN_OCI_EXCEPTIONS

logger = logging.getLogger(__name__)

@shared_task
def sync_oci_data_task(connection_id, days_cost=30):
    """
    Celery background task to synchronize cost, inventory, and metrics from OCI.
    """
    logger.info("Starting background OCI sync task for connection ID %s", connection_id)
    try:
        connection = OCIConnection.objects.get(pk=connection_id)
        sync_service = OCISyncService(connection)
        sync_service.sync_all(days_cost=days_cost)
        logger.info("OCI sync task successfully completed for connection ID %s", connection_id)
    except OCIConnection.DoesNotExist:
        logger.error("OCIConnection with ID %s does not exist. Aborting sync task.", connection_id)
    except KNOWN_OCI_EXCEPTIONS as e:
        from oci_connector.services.oci_client import sanitize_oci_error
        safe_message = sanitize_oci_error(e, operation="scheduled OCI synchronization")
        logger.error("OCI synchronization failed for connection_id=%s: %s", connection_id, safe_message)
    except Exception:
        logger.exception("Unexpected non-OCI error in sync_oci_data_task for connection %s", connection_id)


@shared_task
def sync_all_active_connections_task():
    """
    Periodic task to sync all active OCI connections daily.
    Queries only active OCIConnection records, queues an independent child sync task for each,
    isolates enqueue failures, and does not decrypt keys itself.
    """
    logger.info("Starting periodic sync task for all active OCI connections.")
    active_connection_ids = OCIConnection.objects.filter(is_active=True).values_list("id", flat=True)
    for conn_id in active_connection_ids:
        try:
            logger.info("Enqueuing sync task for OCIConnection ID: %s", conn_id)
            sync_oci_data_task.delay(conn_id)
        except Exception:
            logger.error("Unable to enqueue OCI synchronization for connection ID %s.", conn_id)

