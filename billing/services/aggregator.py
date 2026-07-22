from django.db.models import Sum
from django.db.models.functions import TruncMonth, TruncDay
from billing.models import BillingRecord


class BillingCostAggregator:
    @staticmethod
    def calculate_service_costs(queryset) -> dict[str, float]:
        """Group cost aggregates by service."""
        aggregates = (
            queryset.values("service")
            .annotate(total_cost=Sum("cost"))
            .order_by("-total_cost")
        )
        return {item["service"]: float(item["total_cost"] or 0.0) for item in aggregates}

    @staticmethod
    def calculate_daily_costs(queryset) -> dict[str, float]:
        """Group cost aggregates by day (YYYY-MM-DD)."""
        aggregates = (
            queryset.values("usage_date")
            .annotate(total_cost=Sum("cost"))
            .order_by("usage_date")
        )
        return {
            str(item["usage_date"]): float(item["total_cost"] or 0.0)
            for item in aggregates
            if item["usage_date"] is not None
        }

    @staticmethod
    def calculate_monthly_costs(queryset) -> dict[str, float]:
        """Group cost aggregates by month (YYYY-MM)."""
        # Since usage_date is a DateField, we can slice/extract or use TruncMonth
        # Slicing in Python or using Django QuerySet values is standard.
        # Let's perform aggregation on the database or simple dictionary mapping.
        # Let's load the grouped items or group them via database TruncMonth
        aggregates = (
            queryset.annotate(month=TruncMonth("usage_start"))
            .values("month")
            .annotate(total_cost=Sum("cost"))
            .order_by("month")
        )
        results = {}
        for item in aggregates:
            month_val = item["month"]
            if month_val:
                month_str = month_val.strftime("%Y-%m")
                results[month_str] = float(item["total_cost"] or 0.0)
        return results

    @staticmethod
    def calculate_region_costs(queryset) -> dict[str, float]:
        """Group cost aggregates by region."""
        aggregates = (
            queryset.values("region")
            .annotate(total_cost=Sum("cost"))
            .order_by("-total_cost")
        )
        return {item["region"]: float(item["total_cost"] or 0.0) for item in aggregates}
