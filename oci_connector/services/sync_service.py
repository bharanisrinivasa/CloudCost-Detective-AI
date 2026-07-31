import logging
import hashlib
import datetime
from decimal import Decimal
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from accounts.models import Project
from billing.models import BillingUpload, BillingRecord
from oci_connector.models import (
    OCIConnection,
    OCIComputeInstance,
    OCIVolume,
    OCIObjectStorageBucket,
    OCIPublicIp,
    OCILoadBalancer,
    OCIResourceMetricSummary,
    OCISyncLog,
)
from oci_connector.services.oci_client import build_oci_config, OCIClientFactory, sanitize_oci_error, KNOWN_OCI_EXCEPTIONS
import oci

logger = logging.getLogger(__name__)

SUPPORTED_GROUP_BY = [
    "resourceId",
    "resourceName",
    "service",
    "compartmentId",
    "region",
    "availabilityDomain",
    "unit",
    "currency"
]

METRIC_REGISTRY = {
    "CPU_UTILIZATION": {
        "metric_name": "CpuUtilization",
        "namespace": "oci_computeagent",
        "resource_type": "compute",
        "query": "CpuUtilization[1h].mean()",
        "expected_daily_samples": 24,
    },
    "MEMORY_UTILIZATION": {
        "metric_name": "MemoryUtilization",
        "namespace": "oci_computeagent",
        "resource_type": "compute",
        "query": "MemoryUtilization[1h].mean()",
        "expected_daily_samples": 24,
    },
    "NETWORKS_BYTES_IN": {
        "metric_name": "NetworksBytesIn",
        "namespace": "oci_computeagent",
        "resource_type": "compute",
        "query": "NetworksBytesIn[1h].mean()",
        "expected_daily_samples": 24,
    },
    "NETWORKS_BYTES_OUT": {
        "metric_name": "NetworksBytesOut",
        "namespace": "oci_computeagent",
        "resource_type": "compute",
        "query": "NetworksBytesOut[1h].mean()",
        "expected_daily_samples": 24,
    },
    "VOLUME_READ_THROUGHPUT": {
        "metric_name": "VolumeReadThroughput",
        "namespace": "oci_blockstore",
        "resource_type": "volume",
        "query": "VolumeReadThroughput[1h].mean()",
        "expected_daily_samples": 24,
    },
    "VOLUME_WRITE_THROUGHPUT": {
        "metric_name": "VolumeWriteThroughput",
        "namespace": "oci_blockstore",
        "resource_type": "volume",
        "query": "VolumeWriteThroughput[1h].mean()",
        "expected_daily_samples": 24,
    },
    "LB_ACTIVE_CONNECTIONS": {
        "metric_name": "ActiveConnections",
        "namespace": "oci_lbaas",
        "resource_type": "load_balancer",
        "query": "ActiveConnections[1h].mean()",
        "expected_daily_samples": 24,
    },
}


def paginate_oci_call(client_method, *args, **kwargs):
    """
    Helper to execute OCI SDK list calls and handle multi-page results.
    """
    results = []
    kwargs_copy = kwargs.copy()
    response = client_method(*args, **kwargs_copy)
    if response.data:
        if isinstance(response.data, list):
            results.extend(response.data)
        else:
            results.append(response.data)

    while getattr(response, "has_next_page", False) is True:
        kwargs_copy["page"] = response.next_page
        response = client_method(*args, **kwargs_copy)
        if response.data:
            if isinstance(response.data, list):
                results.extend(response.data)
            else:
                results.append(response.data)
    return results


def generate_source_fingerprint(project_id, resource_id, usage_start, usage_end, service, region, currency):
    """
    Generate a deterministic SHA-256 fingerprint hash for cost records.
    """
    raw_str = f"{project_id}|OCI_API|{resource_id or ''}|{usage_start or ''}|{usage_end or ''}|{service or ''}|{region or ''}|{currency or ''}"
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()


class OCISyncService:
    def __init__(self, connection: OCIConnection):
        self.connection = connection
        self.project = connection.project
        self.config = build_oci_config(connection)
        self.factory = OCIClientFactory(self.config)
        self.sync_log = None
        self.warnings = []
        self.errors = []
        self.records_created = 0
        self.records_updated = 0
        self.success_scopes = {
            "compute": [],
            "volume": [],
            "bucket": [],
            "public_ip": [],
            "load_balancer": [],
        }

    def add_warning(self, msg: str):
        logger.warning(msg)
        self.warnings.append(msg)

    def add_error(self, msg: str):
        logger.error(msg)
        self.errors.append(msg)

    def start_log(self, sync_type: str):
        from django.db import transaction, utils
        from django.core.exceptions import ValidationError
        import datetime

        try:
            with transaction.atomic():
                # Lock the connection row briefly
                conn = OCIConnection.objects.select_for_update(nowait=True).get(pk=self.connection.pk)
                
                # Check for active running sync logs
                active = OCISyncLog.objects.filter(connection=conn, status="PROCESSING").first()
                if active:
                    # Timeout stale sync after 30 mins
                    if timezone.now() - active.started_at > datetime.timedelta(minutes=30):
                        active.status = "FAILED"
                        active.error_summary = "Synchronization timed out or was aborted."
                        active.completed_at = timezone.now()
                        active.save()
                    else:
                        raise ValidationError("Another synchronization task is currently running for this connection.")
                
                self.sync_log = OCISyncLog.objects.create(
                    project=self.project,
                    connection=self.connection,
                    sync_type=sync_type,
                    status="PROCESSING",
                )
        except utils.OperationalError:
            raise ValidationError("Another user or background task is currently running a synchronization for this connection.")

    def finish_log(self, status: str):
        if self.sync_log:
            self.sync_log.status = status
            self.sync_log.records_created = self.records_created
            self.sync_log.records_updated = self.records_updated
            self.sync_log.warning_summary = "\n".join(self.warnings)[:5000]
            self.sync_log.error_summary = "\n".join(self.errors)[:5000]
            self.sync_log.completed_at = timezone.now()
            self.sync_log.save()

    def discover_compartments(self) -> list:
        """
        Retrieves all active descendant compartments starting from root.
        """
        identity_client = self.factory.get_identity_client()
        compartments = []
        
        # Test root compartment first
        try:
            root_comp = identity_client.get_compartment(self.connection.compartment_ocid).data
            compartments.append(root_comp)
        except KNOWN_OCI_EXCEPTIONS as e:
            sanitized = sanitize_oci_error(e)
            self.add_error(f"Failed to read root compartment: {sanitized}")
            return []
        except Exception:
            logger.exception("Unexpected internal error in discover_compartments")
            self.add_error("Failed to read root compartment due to an internal application error.")
            return []

        try:
            subcomps = paginate_oci_call(
                identity_client.list_compartments,
                compartment_id=self.connection.compartment_ocid,
                compartment_id_in_subtree=True,
                access_level="ACCESSIBLE",
            )
            for sc in subcomps:
                if sc.lifecycle_state == "ACTIVE":
                    compartments.append(sc)
        except KNOWN_OCI_EXCEPTIONS as e:
            sanitized = sanitize_oci_error(e)
            self.add_warning(f"Error traversing child compartments: {sanitized}")
        except Exception:
            logger.exception("Unexpected internal error traversing child compartments")
            self.add_warning("Error traversing child compartments due to an internal application error.")

        return compartments

    def get_subscribed_regions(self) -> list:
        """
        Retrieves all subscribed region names. Fallback to configured region.
        """
        identity_client = self.factory.get_identity_client()
        try:
            subscriptions = paginate_oci_call(
                identity_client.list_region_subscriptions,
                self.connection.tenancy_ocid,
            )
            return [sub.region_name for sub in subscriptions]
        except KNOWN_OCI_EXCEPTIONS as e:
            sanitized = sanitize_oci_error(e)
            self.add_warning(f"Failed to list region subscriptions: {sanitized}. Using default region.")
            return [self.connection.region]
        except Exception:
            logger.exception("Unexpected internal error listing region subscriptions")
            self.add_warning("Failed to list region subscriptions due to an internal application error. Using default region.")
            return [self.connection.region]

    def sync_all(self, days_cost=30):
        """
        High-level orchestration of a full sync.
        """
        self.start_log("ALL")
        try:
            # 1. Discover topology
            compartments = self.discover_compartments()
            if not compartments:
                self.finish_log("FAILED")
                return

            regions = self.get_subscribed_regions()

            # 2. Run sync phases
            self.sync_inventory_data(compartments, regions)
            self.sync_cost_data(days_cost)
            self.sync_metrics_data(compartments, regions)

            # Determine final status
            any_success = any(len(scopes) > 0 for scopes in self.success_scopes.values())
            if self.records_created > 0 or self.records_updated > 0:
                any_success = True

            if self.errors and not any_success:
                status = "FAILED"
            elif self.errors or self.warnings:
                status = "PARTIAL"
            else:
                status = "COMPLETED"

            self.finish_log(status)
        except KNOWN_OCI_EXCEPTIONS as e:
            safe_message = sanitize_oci_error(e, operation="synchronization")
            logger.error("%s", safe_message)
            self.add_error(safe_message)
            self.finish_log("FAILED")
        except Exception:
            logger.exception("Unexpected fatal error during OCI synchronization.")
            self.add_error("OCI synchronization failed due to an internal application error.")
            self.finish_log("FAILED")

    def sync_cost_data(self, days=30):
        """
        Sync cost data from Usage API for the last N days.
        Uses idempotency keys and reconciles/upserts existing cost lines.
        """
        usage_client = self.factory.get_usage_client()
        end_time = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start_time = end_time - datetime.timedelta(days=days)

        details = oci.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=self.connection.tenancy_ocid,
            time_usage_started=start_time,
            time_usage_ended=end_time,
            granularity="DAILY",
            query_type="COST",
            group_by=SUPPORTED_GROUP_BY,
        )

        try:
            summarized_usages = paginate_oci_call(
                usage_client.request_summarized_usages,
                details
            )
        except KNOWN_OCI_EXCEPTIONS as e:
            sanitized = sanitize_oci_error(e)
            self.add_warning(f"Cost sync failed: {sanitized}")
            return
        except Exception:
            logger.exception("Unexpected internal error in sync_cost_data")
            self.add_warning("Cost sync failed due to an internal application error.")
            return

        if not summarized_usages:
            return

        with transaction.atomic():
            # Backfill existing OCI API Sync records with the new fingerprint format
            records_to_backfill = BillingRecord.objects.filter(
                upload__upload_type="OCI API Sync",
                upload__project=self.project
            )
            backfill_needed = []
            for rec in records_to_backfill:
                correct_fp = generate_source_fingerprint(
                    self.project.id,
                    rec.resource_id,
                    rec.usage_start.isoformat() if rec.usage_start else "",
                    rec.usage_end.isoformat() if rec.usage_end else "",
                    rec.service,
                    rec.region,
                    rec.currency
                )
                if rec.source_fingerprint != correct_fp:
                    rec.source_fingerprint = correct_fp
                    backfill_needed.append(rec)
            if backfill_needed:
                BillingRecord.objects.bulk_update(backfill_needed, ["source_fingerprint"])

            # Get or create OCI Cost Upload record
            upload_title = f"OCI Cost Sync {start_time.date()} to {end_time.date()}"
            upload, _ = BillingUpload.objects.get_or_create(
                project=self.project,
                upload_type="OCI API Sync",
                original_filename="",
                defaults={
                    "title": upload_title,
                    "upload_status": "Completed",
                    "remarks": f"Synchronized automatically from OCI Usage API at {timezone.now()}",
                    "file_size": 0,
                }
            )

            # Retrieve existing OCI API records to match fingerprints (strictly exclude CSV uploads)
            existing_records = BillingRecord.objects.filter(
                upload__project=self.project,
                upload__upload_type="OCI API Sync",
                usage_start__gte=start_time,
                usage_end__lte=end_time
            ).exclude(source_fingerprint__isnull=True)

            fingerprint_map = {rec.source_fingerprint: rec for rec in existing_records}

            create_list = []
            update_list = []

            for usage in summarized_usages:
                # Usage fields extraction
                resource_id = getattr(usage, "resource_id", None) or "unknown-resource"
                resource_name = getattr(usage, "resource_name", "")
                service = getattr(usage, "service", "Unknown")
                compartment = getattr(usage, "compartment_id", "default")
                region = getattr(usage, "region", self.connection.region)
                ad = getattr(usage, "availability_domain", "")
                usage_start = usage.time_usage_started
                usage_end = usage.time_usage_ended
                qty = Decimal(str(usage.qty or 0.0))
                unit = getattr(usage, "unit", "")
                cost = Decimal(str(usage.amount or 0.0))
                currency = getattr(usage, "currency", "USD") or "USD"

                fingerprint = generate_source_fingerprint(
                    self.project.id,
                    resource_id,
                    usage_start.isoformat() if usage_start else "",
                    usage_end.isoformat() if usage_end else "",
                    service,
                    region,
                    currency
                )

                if fingerprint in fingerprint_map:
                    # Update
                    rec = fingerprint_map[fingerprint]
                    rec.cost = cost
                    rec.amount = cost
                    rec.usage_quantity = qty
                    rec.resource_name = resource_name
                    update_list.append(rec)
                    self.records_updated += 1
                else:
                    # Create
                    rec = BillingRecord(
                        upload=upload,
                        service=service,
                        resource_name=resource_name,
                        resource_id=resource_id,
                        compartment=compartment,
                        region=region,
                        availability_domain=ad,
                        usage_start=usage_start,
                        usage_end=usage_end,
                        usage_quantity=qty,
                        usage_unit=unit,
                        cost=cost,
                        currency=currency,
                        amount=cost,
                        source_fingerprint=fingerprint
                    )
                    if hasattr(usage_start, "date"):
                        rec.usage_date = usage_start.date()
                    else:
                        rec.usage_date = usage_start
                    
                    create_list.append(rec)
                    self.records_created += 1

            if create_list:
                BillingRecord.objects.bulk_create(create_list)
            if update_list:
                BillingRecord.objects.bulk_update(update_list, ["cost", "amount", "usage_quantity", "resource_name"])

    def sync_inventory_data(self, compartments, regions):
        """
        Coordinates sync across all inventory models.
        """
        # Iterators
        for region in regions:
            # Create regional clients
            compute_client = self.factory.get_compute_client(region)
            block_client = self.factory.get_blockstorage_client(region)
            network_client = self.factory.get_virtual_network_client(region)
            lb_client = self.factory.get_load_balancer_client(region)
            object_client = self.factory.get_object_storage_client(region)

            for comp in compartments:
                # 1. Compute
                try:
                    instances = paginate_oci_call(
                        compute_client.list_instances,
                        compartment_id=comp.id
                    )
                    self.process_compute_instances(instances, region, comp.id)
                    self.success_scopes["compute"].append((region, comp.id))
                except KNOWN_OCI_EXCEPTIONS as e:
                    sanitized = sanitize_oci_error(e)
                    self.add_warning(f"Failed to query Compute in {region}/{comp.name}: {sanitized}")
                except Exception:
                    logger.exception("Unexpected internal error in sync_inventory_data")
                    self.add_warning(f"Failed to query Compute in {region}/{comp.name} due to an internal application error.")

                # 2. Volumes
                try:
                    volumes = paginate_oci_call(block_client.list_volumes, compartment_id=comp.id)
                    boot_volumes = paginate_oci_call(block_client.list_boot_volumes, compartment_id=comp.id)
                    
                    # Try attachments mapping
                    attachments = []
                    attachment_sync_ok = False
                    try:
                        attachments = paginate_oci_call(compute_client.list_volume_attachments, compartment_id=comp.id)
                        attachment_sync_ok = True
                    except KNOWN_OCI_EXCEPTIONS as e:
                        sanitized = sanitize_oci_error(e)
                        self.add_warning(f"Failed attachments mapping in {region}/{comp.name}: {sanitized}")
                    except Exception:
                        logger.exception("Unexpected internal error in list_volume_attachments")
                        self.add_warning(f"Failed attachments mapping in {region}/{comp.name} due to an internal application error.")

                    self.process_volumes(volumes, boot_volumes, attachments, attachment_sync_ok, region, comp.id)
                    self.success_scopes["volume"].append((region, comp.id))
                except KNOWN_OCI_EXCEPTIONS as e:
                    sanitized = sanitize_oci_error(e)
                    self.add_warning(f"Failed to query Volumes in {region}/{comp.name}: {sanitized}")
                except Exception:
                    logger.exception("Unexpected internal error in volumes query")
                    self.add_warning(f"Failed to query Volumes in {region}/{comp.name} due to an internal application error.")

                # 3. Public IPs
                try:
                    regional_ips = paginate_oci_call(network_client.list_public_ips, scope="REGION", compartment_id=comp.id)
                    ad_ips = []
                    try:
                        ad_ips = paginate_oci_call(network_client.list_public_ips, scope="AVAILABILITY_DOMAIN", compartment_id=comp.id)
                    except KNOWN_OCI_EXCEPTIONS as e:
                        sanitized = sanitize_oci_error(e)
                        self.add_warning(f"Failed availability domain IPs querying: {sanitized}")
                    except Exception:
                        logger.exception("Unexpected internal error querying AD IPs")
                        self.add_warning("Failed availability domain IPs querying due to an internal application error.")
                    
                    self.process_public_ips(regional_ips + ad_ips, region, comp.id)
                    self.success_scopes["public_ip"].append((region, comp.id))
                except KNOWN_OCI_EXCEPTIONS as e:
                    sanitized = sanitize_oci_error(e)
                    self.add_warning(f"Failed to query Public IPs in {region}/{comp.name}: {sanitized}")
                except Exception:
                    logger.exception("Unexpected internal error in public IPs query")
                    self.add_warning(f"Failed to query Public IPs in {region}/{comp.name} due to an internal application error.")

                # 4. Load Balancers
                try:
                    lbs = paginate_oci_call(lb_client.list_load_balancers, compartment_id=comp.id)
                    self.process_load_balancers(lbs, region, comp.id)
                    self.success_scopes["load_balancer"].append((region, comp.id))
                except KNOWN_OCI_EXCEPTIONS as e:
                    sanitized = sanitize_oci_error(e)
                    self.add_warning(f"Failed to query Load Balancers in {region}/{comp.name}: {sanitized}")
                except Exception:
                    logger.exception("Unexpected internal error in load balancers query")
                    self.add_warning(f"Failed to query Load Balancers in {region}/{comp.name} due to an internal application error.")

                # 5. Object Storage Buckets
                try:
                    namespace = object_client.get_namespace().data
                    buckets = paginate_oci_call(object_client.list_buckets, namespace, compartment_id=comp.id)
                    self.process_buckets(buckets, object_client, namespace, region, comp.id)
                    self.success_scopes["bucket"].append((region, comp.id))
                except KNOWN_OCI_EXCEPTIONS as e:
                    sanitized = sanitize_oci_error(e)
                    self.add_warning(f"Failed to query Storage Buckets in {region}/{comp.name}: {sanitized}")
                except Exception:
                    logger.exception("Unexpected internal error in buckets query")
                    self.add_warning(f"Failed to query Storage Buckets in {region}/{comp.name} due to an internal application error.")

        # 3. Perform presence cleanup (fail closed / preserve unknown on sync error)
        self.clean_absent_resources()

    def process_compute_instances(self, instances, region, compartment_id):
        now = timezone.now()
        for inst in instances:
            if inst.lifecycle_state == "TERMINATED":
                continue
            
            ocpus = None
            memory_in_gbs = None
            if hasattr(inst, "shape_config") and inst.shape_config:
                ocpus = Decimal(str(inst.shape_config.ocpus)) if inst.shape_config.ocpus else None
                memory_in_gbs = Decimal(str(inst.shape_config.memory_in_gbs)) if inst.shape_config.memory_in_gbs else None

            obj, created = OCIComputeInstance.objects.update_or_create(
                project=self.project,
                ocid=inst.id,
                defaults={
                    "connection": self.connection,
                    "name": inst.display_name,
                    "state": inst.lifecycle_state,
                    "shape": inst.shape,
                    "ocpus": ocpus,
                    "memory_in_gbs": memory_in_gbs,
                    "region": region,
                    "compartment_id": compartment_id,
                    "last_seen_at": now,
                    "inventory_status": "PRESENT",
                }
            )
            if created:
                self.records_created += 1
            else:
                self.records_updated += 1

    def process_volumes(self, volumes, boot_volumes, attachments, attachment_sync_ok, region, compartment_id):
        now = timezone.now()
        attachment_map = {}
        if attachment_sync_ok:
            attachment_map = {att.volume_id: att.instance_id for att in attachments if att.lifecycle_state == "ATTACHED"}

        def process_single(vol, vol_type):
            att_state = "UNKNOWN"
            inst_id = None
            if attachment_sync_ok:
                if vol.id in attachment_map:
                    att_state = "ATTACHED"
                    inst_id = attachment_map[vol.id]
                else:
                    att_state = "DETACHED"

            obj, created = OCIVolume.objects.update_or_create(
                project=self.project,
                ocid=vol.id,
                defaults={
                    "connection": self.connection,
                    "name": vol.display_name,
                    "volume_type": vol_type,
                    "state": vol.lifecycle_state,
                    "size_in_gbs": vol.size_in_gbs,
                    "attachment_state": att_state,
                    "attached_instance_id": inst_id,
                    "region": region,
                    "compartment_id": compartment_id,
                    "last_seen_at": now,
                    "inventory_status": "PRESENT",
                }
            )
            if created:
                self.records_created += 1
            else:
                self.records_updated += 1

        for v in volumes:
            if v.lifecycle_state != "TERMINATED":
                process_single(v, "BLOCK")

        for bv in boot_volumes:
            if bv.lifecycle_state != "TERMINATED":
                process_single(bv, "BOOT")

    def process_public_ips(self, public_ips, region, compartment_id):
        now = timezone.now()
        for ip in public_ips:
            if ip.lifecycle_state == "TERMINATED":
                continue
            
            # Orphan condition
            is_orphan = False
            if not ip.assigned_entity_id or ip.assigned_entity_id == "":
                # Reserved unassigned public IP
                if ip.lifecycle_state in ["AVAILABLE", "UNASSIGNED"]:
                    is_orphan = True

            obj, created = OCIPublicIp.objects.update_or_create(
                project=self.project,
                ocid=ip.id,
                defaults={
                    "connection": self.connection,
                    "ip_address": ip.ip_address,
                    "scope": ip.scope,
                    "lifecycle_state": ip.lifecycle_state,
                    "assigned_entity_type": ip.assigned_entity_type,
                    "assigned_entity_id": ip.assigned_entity_id,
                    "is_orphan": is_orphan,
                    "region": region,
                    "compartment_id": compartment_id,
                    "last_seen_at": now,
                    "inventory_status": "PRESENT",
                }
            )
            if created:
                self.records_created += 1
            else:
                self.records_updated += 1

    def process_load_balancers(self, lbs, region, compartment_id):
        now = timezone.now()
        for lb in lbs:
            # IP address extraction
            ips = []
            if hasattr(lb, "ip_addresses") and lb.ip_addresses:
                ips = [ip.ip_address for ip in lb.ip_addresses]

            obj, created = OCILoadBalancer.objects.update_or_create(
                project=self.project,
                ocid=lb.id,
                defaults={
                    "connection": self.connection,
                    "name": lb.display_name,
                    "shape": lb.shape_name,
                    "state": lb.lifecycle_state,
                    "is_private": lb.is_private,
                    "ip_addresses": ips,
                    "region": region,
                    "compartment_id": compartment_id,
                    "last_seen_at": now,
                    "inventory_status": "PRESENT",
                }
            )
            if created:
                self.records_created += 1
            else:
                self.records_updated += 1

    def process_buckets(self, buckets, object_client, namespace, region, compartment_id):
        now = timezone.now()
        for b_summary in buckets:
            size = None
            count = None
            tier = None

            try:
                bucket_detail = object_client.get_bucket(namespace, b_summary.name)
                size = bucket_detail.data.approximate_size
                count = bucket_detail.data.approximate_count
                tier = getattr(bucket_detail.data, "storage_tier", "Standard")
            except Exception:
                pass

            obj, created = OCIObjectStorageBucket.objects.update_or_create(
                project=self.project,
                namespace=namespace,
                name=b_summary.name,
                defaults={
                    "connection": self.connection,
                    "approximate_size": size,
                    "approximate_count": count,
                    "storage_tier": tier,
                    "region": region,
                    "compartment_id": compartment_id,
                    "last_seen_at": now,
                    "inventory_status": "PRESENT",
                }
            )
            if created:
                self.records_created += 1
            else:
                self.records_updated += 1

    def clean_absent_resources(self):
        """
        Marks resources as ABSENT if they are missing from authoritative sync scopes.
        Does not touch resources in regions/compartments where the scan failed.
        """
        def get_scope_filter(scopes):
            if not scopes:
                return Q(pk__in=[])
            q_obj = Q()
            for reg, comp in scopes:
                q_obj |= Q(region=reg, compartment_id=comp)
            return q_obj

        cutoff = timezone.now() - datetime.timedelta(minutes=5)

        # 1. Compute
        if self.success_scopes.get("compute"):
            scope_filter = get_scope_filter(self.success_scopes["compute"])
            OCIComputeInstance.objects.filter(
                project=self.project,
                connection=self.connection
            ).filter(scope_filter).filter(
                last_seen_at__lt=cutoff
            ).update(inventory_status="ABSENT")

        # 2. Volumes
        if self.success_scopes.get("volume"):
            scope_filter = get_scope_filter(self.success_scopes["volume"])
            OCIVolume.objects.filter(
                project=self.project,
                connection=self.connection
            ).filter(scope_filter).filter(
                last_seen_at__lt=cutoff
            ).update(inventory_status="ABSENT")

        # 3. Public IPs
        if self.success_scopes.get("public_ip"):
            scope_filter = get_scope_filter(self.success_scopes["public_ip"])
            OCIPublicIp.objects.filter(
                project=self.project,
                connection=self.connection
            ).filter(scope_filter).filter(
                last_seen_at__lt=cutoff
            ).update(inventory_status="ABSENT")

        # 4. Load Balancers
        if self.success_scopes.get("load_balancer"):
            scope_filter = get_scope_filter(self.success_scopes["load_balancer"])
            OCILoadBalancer.objects.filter(
                project=self.project,
                connection=self.connection
            ).filter(scope_filter).filter(
                last_seen_at__lt=cutoff
            ).update(inventory_status="ABSENT")

        # 5. Buckets
        if self.success_scopes.get("bucket"):
            scope_filter = get_scope_filter(self.success_scopes["bucket"])
            OCIObjectStorageBucket.objects.filter(
                project=self.project,
                connection=self.connection
            ).filter(scope_filter).filter(
                last_seen_at__lt=cutoff
            ).update(inventory_status="ABSENT")

    def sync_metrics_data(self, compartments, regions):
        """
        Gathers daily average, daily maximum, and coverage ratios for metrics in logical definitions registry.
        """
        now = timezone.now()
        yesterday = now - datetime.timedelta(days=1)
        target_date = yesterday.date()

        for region in regions:
            monitoring_client = self.factory.get_monitoring_client(region)
            for comp in compartments:
                for metric in METRIC_REGISTRY.values():
                    try:
                        details = oci.monitoring.models.SummarizeMetricsDataDetails(
                            namespace=metric["namespace"],
                            query=metric["query"],
                            start_time=yesterday,
                            end_time=now,
                        )
                        response = monitoring_client.summarize_metrics_data(comp.id, details)
                        
                        if not response.data:
                            continue

                        for md in response.data:
                            res_id = md.dimensions.get("resourceId")
                            if not res_id:
                                continue

                            if not md.datapoints:
                                continue

                            values = [Decimal(str(dp.value)) for dp in md.datapoints]
                            avg_val = sum(values) / len(values)
                            max_val = max(values)
                            min_val = min(values)
                            sample_cnt = len(values)
                            
                            # Telemetry coverage ratio using expected sample count from definition
                            expected_samples = metric["expected_daily_samples"]
                            coverage = Decimal(sample_cnt) / Decimal(expected_samples)
                            if coverage > Decimal("1.00"):
                                coverage = Decimal("1.00")
                            elif coverage < Decimal("0.00"):
                                coverage = Decimal("0.00")

                            OCIResourceMetricSummary.objects.update_or_create(
                                project=self.project,
                                resource_id=res_id,
                                metric_name=metric["metric_name"],
                                date=target_date,
                                defaults={
                                    "connection": self.connection,
                                    "average_value": avg_val,
                                    "maximum_value": max_val,
                                    "minimum_value": min_val,
                                    "sample_count": sample_cnt,
                                    "coverage_ratio": coverage,
                                }
                            )
                    except KNOWN_OCI_EXCEPTIONS as e:
                        sanitized = sanitize_oci_error(e)
                        self.add_warning(
                            f"Metrics sync warning: {metric['metric_name']} failed in {region}/{comp.name}: {sanitized}"
                        )
                    except Exception:
                        logger.exception("Unexpected internal error in sync_metrics_data")
                        self.add_warning(
                            f"Metrics sync warning: {metric['metric_name']} failed in {region}/{comp.name} due to an internal application error."
                        )
