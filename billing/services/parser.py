import csv
import io
import datetime
from decimal import Decimal
from django.utils import timezone
from django.conf import settings
from billing.models import BillingRecord, BillingUpload
from .validator import CSVHeaderValidator


class BillingCSVParser:
    @staticmethod
    def parse_date(date_str: str | None) -> datetime.datetime | None:
        """Parse OCI and standard date formats."""
        if not date_str:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.datetime.strptime(date_str.strip(), fmt)
            except ValueError:
                continue
        # ISO format fallback
        try:
            return datetime.datetime.fromisoformat(date_str.strip().replace('Z', '+00:00'))
        except Exception:
            return None

    @classmethod
    def make_dt_aware(cls, dt: datetime.datetime | None) -> datetime.datetime | None:
        """Ensure naive datetimes are marked timezone-aware if Django USE_TZ is enabled."""
        if dt is None:
            return None
        if getattr(settings, 'USE_TZ', False) and timezone.is_naive(dt):
            return timezone.make_aware(dt)
        return dt

    @classmethod
    def parse(cls, text_content: str, upload: BillingUpload) -> dict:
        """
        Parse OCI Cost / Usage CSV contents.
        Returns:
            {
                "records": list[BillingRecord],
                "logs": list[str],
                "rows_read": int,
                "rows_imported": int,
                "rows_skipped": int
            }
        """
        logs = []
        
        def log(msg: str):
            timestamp = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
            logs.append(f"[{timestamp}] {msg}")

        # Parse CSV lines
        reader = csv.reader(io.StringIO(text_content))
        headers = next(reader, None)
        if not headers:
            raise ValueError("CSV file is empty or missing headers.")

        # Map headers
        col_map = CSVHeaderValidator.validate(headers)
        log(f"Header validation passed. Synonyms mapped successfully.")

        rows_read = 0
        rows_imported = 0
        rows_skipped = 0
        seen_fingerprints = set()
        records = []

        line_number = 1
        for row in reader:
            line_number += 1
            if not row or all(cell.strip() == "" for cell in row):
                continue  # skip completely blank rows

            rows_read += 1

            def get_val(field_key: str, default: str = "") -> str:
                idx = col_map.get(field_key)
                if idx is not None and idx < len(row):
                    return row[idx].strip()
                return default

            service_val = get_val("service", "Unknown")
            cost_val_raw = get_val("cost", "0")
            resource_id_val = get_val("resource_id", "unknown-resource")
            resource_name_val = get_val("resource_name", "")
            compartment_val = get_val("compartment", "default")
            region_val = get_val("region", "unknown-region")
            ad_val = get_val("availability_domain", "")
            usage_start_raw = get_val("usage_start", "")
            usage_end_raw = get_val("usage_end", "")
            usage_qty_raw = get_val("usage_quantity", "0")
            usage_unit_val = get_val("usage_unit", "")
            currency_val = get_val("currency", "USD")
            tags_val = get_val("tags", "")

            # Parse start date
            usage_start_parsed = cls.parse_date(usage_start_raw)
            if not usage_start_parsed:
                # Fallback to usage_date
                usage_start_parsed = cls.parse_date(get_val("usage_date"))

            usage_start_dt = cls.make_dt_aware(usage_start_parsed)
            usage_end_parsed = cls.parse_date(usage_end_raw)
            usage_end_dt = cls.make_dt_aware(usage_end_parsed)

            # Auto-calculate end if missing
            if usage_start_dt and not usage_end_dt:
                usage_end_dt = usage_start_dt + datetime.timedelta(hours=24)

            # Parse cost
            try:
                clean_cost = cost_val_raw.replace("$", "").replace(",", "").strip()
                cost_decimal = Decimal(clean_cost)
            except Exception:
                rows_skipped += 1
                log(f"Row {line_number} skipped: Invalid numeric cost '{cost_val_raw}'")
                continue

            # Parse quantity
            try:
                clean_qty = usage_qty_raw.replace(",", "").strip()
                qty_decimal = Decimal(clean_qty)
            except Exception:
                qty_decimal = Decimal("0.0")

            # Validate basic parameters
            if not service_val or not resource_id_val:
                rows_skipped += 1
                log(f"Row {line_number} skipped: Missing service or resource ID value")
                continue

            if not usage_start_dt:
                rows_skipped += 1
                log(f"Row {line_number} skipped: Missing or invalid usage date/timestamp '{usage_start_raw}'")
                continue

            # Fingerprint to skip duplicates in this import batch
            fingerprint = (
                resource_id_val,
                usage_start_dt.isoformat() if hasattr(usage_start_dt, 'isoformat') else str(usage_start_dt),
                str(cost_decimal),
                service_val
            )
            if fingerprint in seen_fingerprints:
                rows_skipped += 1
                log(f"Row {line_number} skipped: Duplicate row detected (resource_id: {resource_id_val}, cost: {cost_decimal})")
                continue

            seen_fingerprints.add(fingerprint)

            # Instantiate database model object (not yet saved to DB)
            record = BillingRecord(
                upload=upload,
                service=service_val,
                resource_name=resource_name_val,
                resource_id=resource_id_val,
                compartment=compartment_val,
                region=region_val,
                availability_domain=ad_val,
                usage_start=usage_start_dt,
                usage_end=usage_end_dt,
                usage_quantity=qty_decimal,
                usage_unit=usage_unit_val,
                cost=cost_decimal,
                currency=currency_val,
                tags=tags_val,
                amount=cost_decimal
            )
            if hasattr(usage_start_dt, 'date'):
                record.usage_date = usage_start_dt.date()
            else:
                record.usage_date = usage_start_dt

            records.append(record)
            rows_imported += 1

        return {
            "records": records,
            "logs": logs,
            "rows_read": rows_read,
            "rows_imported": rows_imported,
            "rows_skipped": rows_skipped
        }
