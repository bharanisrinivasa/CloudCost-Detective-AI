import logging
import oci
import socket
from django.core.exceptions import ValidationError, ImproperlyConfigured
from cryptography.fernet import InvalidToken
import binascii

from oci.identity import IdentityClient
from oci.usage_api import UsageapiClient
from oci.core import ComputeClient, BlockstorageClient, VirtualNetworkClient
from oci.object_storage import ObjectStorageClient
from oci.load_balancer import LoadBalancerClient
from oci.monitoring import MonitoringClient
from oci.retry import DEFAULT_RETRY_STRATEGY

from oci_connector.services.encryption import decrypt_private_key

logger = logging.getLogger(__name__)

# Expected cryptography/private-key decryption failures
EXPECTED_CRYPTOGRAPHY_EXCEPTIONS = (InvalidToken, ImproperlyConfigured, binascii.Error)

# Centralized tuple of known OCI/network/configuration exceptions
_known_classes = [ValidationError]
for name in ["ServiceError", "ClientError", "InvalidConfig", "InvalidPrivateKey", "RequestException", "ConnectTimeout", "ReadTimeout"]:
    if hasattr(oci.exceptions, name):
        _known_classes.append(getattr(oci.exceptions, name))

try:
    import requests.exceptions
    for name in ["Timeout", "ConnectionError", "RequestException"]:
        if hasattr(requests.exceptions, name):
            _known_classes.append(getattr(requests.exceptions, name))
except ImportError:
    pass

_known_classes.extend([socket.timeout, socket.gaierror, TimeoutError, ConnectionError])

KNOWN_OCI_EXCEPTIONS = tuple(dict.fromkeys(_known_classes))

def build_oci_config(connection) -> dict:
    """
    Builds the OCI SDK configuration dictionary in memory.
    Decrypts the private key and validates the config structure.
    """
    try:
        decrypted_key = decrypt_private_key(connection.private_key_encrypted)
    except EXPECTED_CRYPTOGRAPHY_EXCEPTIONS:
        logger.error("OCI credential decryption failed.")
        raise ValidationError("OCI Connection credentials cannot be decrypted. Check OCI_ENCRYPTION_KEY.")

    config = {
        "tenancy": connection.tenancy_ocid,
        "user": connection.user_ocid,
        "fingerprint": connection.fingerprint,
        "key_content": decrypted_key,
        "region": connection.region,
    }

    try:
        oci.config.validate_config(config)
    except KNOWN_OCI_EXCEPTIONS as e:
        safe_message = sanitize_oci_error(e, operation="configuration validation")
        logger.error("%s", safe_message)
        raise ValidationError("OCI Connection configuration is invalid according to the OCI SDK.")
    except Exception:
        logger.exception("Unexpected internal error during OCI operation.")
        raise ValidationError("OCI Connection configuration validation failed due to an internal error.")

    return config


class OCIClientFactory:
    """
    Centralized client construction utilizing OCI SDK default retry strategy
    to gracefully handle throttling (HTTP 429) and transient errors.
    """
    def __init__(self, config: dict):
        self.config = config
        self.retry_strategy = DEFAULT_RETRY_STRATEGY

    def get_identity_client(self) -> IdentityClient:
        return IdentityClient(self.config, retry_strategy=self.retry_strategy)

    def get_usage_client(self) -> UsageapiClient:
        return UsageapiClient(self.config, retry_strategy=self.retry_strategy)

    def get_compute_client(self, region=None) -> ComputeClient:
        cfg = self.config.copy()
        if region:
            cfg["region"] = region
        return ComputeClient(cfg, retry_strategy=self.retry_strategy)

    def get_blockstorage_client(self, region=None) -> BlockstorageClient:
        cfg = self.config.copy()
        if region:
            cfg["region"] = region
        return BlockstorageClient(cfg, retry_strategy=self.retry_strategy)

    def get_object_storage_client(self, region=None) -> ObjectStorageClient:
        cfg = self.config.copy()
        if region:
            cfg["region"] = region
        return ObjectStorageClient(cfg, retry_strategy=self.retry_strategy)

    def get_virtual_network_client(self, region=None) -> VirtualNetworkClient:
        cfg = self.config.copy()
        if region:
            cfg["region"] = region
        return VirtualNetworkClient(cfg, retry_strategy=self.retry_strategy)

    def get_load_balancer_client(self, region=None) -> LoadBalancerClient:
        cfg = self.config.copy()
        if region:
            cfg["region"] = region
        return LoadBalancerClient(cfg, retry_strategy=self.retry_strategy)

    def get_monitoring_client(self, region=None) -> MonitoringClient:
        cfg = self.config.copy()
        if region:
            cfg["region"] = region
        return MonitoringClient(cfg, retry_strategy=self.retry_strategy)


def sanitize_oci_error(exception, operation=None) -> str:
    """
    Centralized helper to sanitize OCI exceptions. Returns safe, user-friendly
    error messages that hide credential details, fingerprints, keys, and internal URLs.
    """
    if not exception:
        return "Unable to complete the OCI operation."

    # 1. Check ServiceError status code
    if hasattr(exception, "status"):
        try:
            status = int(exception.status)
            if status in [401, 403]:
                return "OCI authentication or authorization failed."
            elif status == 429:
                return "OCI request was throttled. Please retry later."
            elif status >= 500:
                return "OCI service is temporarily unavailable."
        except Exception:
            pass

    # 2. Check for ValidationError or configuration validation errors
    if isinstance(exception, ValidationError):
        return "OCI authentication or configuration validation failed."

    # 3. Check for specific OCI/network exception types
    import socket
    oci_config_classes = []
    for name in ["ClientError", "InvalidConfig", "InvalidPrivateKey"]:
        if hasattr(oci.exceptions, name):
            oci_config_classes.append(getattr(oci.exceptions, name))
            
    if oci_config_classes and isinstance(exception, tuple(oci_config_classes)):
        return "OCI authentication or configuration validation failed."

    timeout_classes = [socket.timeout, socket.gaierror, TimeoutError, ConnectionError]
    for name in ["RequestException", "ConnectTimeout", "ReadTimeout"]:
        if hasattr(oci.exceptions, name):
            timeout_classes.append(getattr(oci.exceptions, name))
    try:
        import requests.exceptions
        for name in ["Timeout", "ConnectionError", "RequestException"]:
            if hasattr(requests.exceptions, name):
                timeout_classes.append(getattr(requests.exceptions, name))
    except ImportError:
        pass
        
    if isinstance(exception, tuple(timeout_classes)):
        return "OCI request timed out or could not reach the service."

    # 4. Substring inspection only for classification, never returned
    err_str = ""
    try:
        err_str = str(exception).lower()
    except Exception:
        pass

    if err_str:
        if "auth" in err_str or "unauthorized" in err_str or "credential" in err_str or "fingerprint" in err_str or "key" in err_str or "permission" in err_str:
            return "OCI authentication or authorization failed."
        elif "throttle" in err_str or "rate limit" in err_str or "429" in err_str:
            return "OCI request was throttled. Please retry later."
        elif "timeout" in err_str or "connection" in err_str or "unavailable" in err_str or "gaierror" in err_str:
            return "OCI request timed out or could not reach the service."
        elif "config" in err_str or "invalid" in err_str:
            return "OCI authentication or configuration validation failed."

    if operation:
        return "OCI synchronization failed for this resource scope."
    return "Unable to complete the OCI operation."


def test_oci_connection_stages(connection) -> dict:
    """
    Tests OCI Connection in stages and performs IAM capability checks.
    Returns a dictionary of stages and their status ("OK" or warning/failure details).
    
    If Authentication or Configuration fails, it is fatal.
    Individual service permission failures are treated as warnings.
    """
    results = {
        "Configuration": "OK",
        "Authentication": "OK",
        "Tenancy Access": "OK",
        "Region Access": "OK",
        "Compartment Access": "OK",
        "Usage API": "OK",
        "Compute Inventory": "OK",
        "Block Storage": "OK",
        "Object Storage": "OK",
        "Networking": "OK",
        "Monitoring": "OK",
        "Load Balancers": "OK",
    }

    try:
        config = build_oci_config(connection)
        factory = OCIClientFactory(config)
    except KNOWN_OCI_EXCEPTIONS as e:
        safe_message = sanitize_oci_error(e, operation="test configuration build")
        logger.error("%s", safe_message)
        results["Configuration"] = "FAILED: OCI authentication or configuration validation failed."
        for k in results:
            if k != "Configuration":
                results[k] = "FAILED"
        return results
    except Exception:
        logger.exception("Unexpected internal error during OCI operation.")
        results["Configuration"] = "FAILED: OCI authentication or configuration validation failed."
        for k in results:
            if k != "Configuration":
                results[k] = "FAILED"
        return results

    # 1. Test Authentication & Tenancy Access (Fatal if fails)
    identity_client = factory.get_identity_client()
    try:
        identity_client.get_tenancy(connection.tenancy_ocid)
    except KNOWN_OCI_EXCEPTIONS as e:
        safe_message = sanitize_oci_error(e, operation="authentication check")
        logger.error("%s", safe_message)
        sanitized = sanitize_oci_error(e)
        results["Authentication"] = f"FAILED: {sanitized}"
        results["Tenancy Access"] = f"FAILED: {sanitized}"
        for k in results:
            if k not in ["Configuration", "Authentication", "Tenancy Access"]:
                results[k] = "FAILED"
        return results
    except Exception:
        logger.exception("Unexpected internal error during OCI operation.")
        results["Authentication"] = "FAILED: OCI authentication or authorization failed."
        results["Tenancy Access"] = "FAILED: OCI authentication or authorization failed."
        for k in results:
            if k not in ["Configuration", "Authentication", "Tenancy Access"]:
                results[k] = "FAILED"
        return results

    # Helper function to test a specific list/check call
    def test_stage(client_name, test_func, *args, **kwargs) -> str:
        try:
            test_func(*args, **kwargs)
            return "OK"
        except oci.exceptions.ServiceError as e:
            if e.status == 403:
                return "WARNING: Authorization/IAM Permission missing."
            elif e.status == 404:
                return "WARNING: Endpoint or resource not found."
            else:
                return f"WARNING: OCI service error {e.status}."
        except KNOWN_OCI_EXCEPTIONS as e:
            safe_message = sanitize_oci_error(e, operation=f"stage testing for {client_name}")
            logger.error("%s", safe_message)
            return "WARNING: Connection failed."
        except Exception:
            logger.exception("Unexpected internal error during OCI operation.")
            return "WARNING: Connection failed."

    # 2. Test Region Access
    results["Region Access"] = test_stage(
        "IdentityClient",
        identity_client.list_region_subscriptions,
        connection.tenancy_ocid
    )

    # 3. Test Compartment Access
    results["Compartment Access"] = test_stage(
        "IdentityClient",
        identity_client.get_compartment,
        connection.compartment_ocid
    )

    # 4. Test Usage API
    usage_client = factory.get_usage_client()
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc)
    yesterday = today - datetime.timedelta(days=1)
    details = oci.usage_api.models.RequestSummarizedUsagesDetails(
        tenant_id=connection.tenancy_ocid,
        time_usage_started=yesterday,
        time_usage_ended=today,
        granularity="DAILY",
        query_type="COST",
        group_by=["service"],
    )
    results["Usage API"] = test_stage(
        "UsageapiClient",
        usage_client.request_summarized_usages,
        details,
        limit=1
    )

    # 5. Test Compute
    compute_client = factory.get_compute_client()
    results["Compute Inventory"] = test_stage(
        "ComputeClient",
        compute_client.list_instances,
        connection.compartment_ocid,
        limit=1
    )

    # 6. Test Block Storage
    blockstorage_client = factory.get_blockstorage_client()
    results["Block Storage"] = test_stage(
        "BlockstorageClient",
        blockstorage_client.list_volumes,
        connection.compartment_ocid,
        limit=1
    )

    # 7. Test Object Storage
    object_storage_client = factory.get_object_storage_client()
    try:
        namespace = object_storage_client.get_namespace()
        results["Object Storage"] = test_stage(
            "ObjectStorageClient",
            object_storage_client.list_buckets,
            namespace.data,
            connection.compartment_ocid,
            limit=1
        )
    except KNOWN_OCI_EXCEPTIONS as e:
        safe_message = sanitize_oci_error(e, operation="object storage namespace retrieval")
        logger.error("%s", safe_message)
        results["Object Storage"] = "WARNING: Object storage namespace could not be retrieved."
    except Exception:
        logger.exception("Unexpected internal error during OCI operation.")
        results["Object Storage"] = "WARNING: Object storage namespace could not be retrieved."

    # 8. Test Networking
    vcn_client = factory.get_virtual_network_client()
    results["Networking"] = test_stage(
        "VirtualNetworkClient",
        vcn_client.list_public_ips,
        scope="REGION",
        compartment_id=connection.compartment_ocid,
        limit=1
    )

    # 9. Test Load Balancers
    lb_client = factory.get_load_balancer_client()
    results["Load Balancers"] = test_stage(
        "LoadBalancerClient",
        lb_client.list_load_balancers,
        connection.compartment_ocid,
        limit=1
    )

    # 10. Test Monitoring
    monitoring_client = factory.get_monitoring_client()
    details = oci.monitoring.models.SummarizeMetricsDataDetails(
        namespace="oci_computeagent",
        query="CpuUtilization[1h].mean()",
        start_time=yesterday,
        end_time=today,
    )
    results["Monitoring"] = test_stage(
        "MonitoringClient",
        monitoring_client.summarize_metrics_data,
        connection.compartment_ocid,
        details
    )

    return results
