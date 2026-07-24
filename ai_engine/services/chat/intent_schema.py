import re
import datetime
from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum
from django.core.exceptions import ValidationError

# --- CORE INTENTS & TIME RANGES ---

class IntentEnum(str, Enum):
    TOTAL_COST = "TOTAL_COST"
    SERVICE_COST = "SERVICE_COST"
    REGION_COST = "REGION_COST"
    RESOURCE_COST = "RESOURCE_COST"
    TOP_SERVICES = "TOP_SERVICES"
    TOP_REGIONS = "TOP_REGIONS"
    TOP_RESOURCES = "TOP_RESOURCES"
    COST_TREND = "COST_TREND"
    COST_COMPARISON = "COST_COMPARISON"
    ANOMALIES = "ANOMALIES"
    WASTE_FINDINGS = "WASTE_FINDINGS"
    POTENTIAL_SAVINGS = "POTENTIAL_SAVINGS"
    COST_INCREASE_EXPLANATION = "COST_INCREASE_EXPLANATION"
    HELP = "HELP"


class TimeRangeTypeEnum(str, Enum):
    ALL_TIME = "ALL_TIME"
    TODAY = "TODAY"
    YESTERDAY = "YESTERDAY"
    THIS_WEEK = "THIS_WEEK"
    LAST_WEEK = "LAST_WEEK"
    THIS_MONTH = "THIS_MONTH"
    LAST_MONTH = "LAST_MONTH"
    LAST_30_DAYS = "LAST_30_DAYS"
    CUSTOM = "CUSTOM"


class TimeRangeSchema(BaseModel):
    type: TimeRangeTypeEnum = Field(description="Standardized time range type.")
    start_date: Optional[str] = Field(None, description="ISO format date (YYYY-MM-DD) for CUSTOM start.")
    end_date: Optional[str] = Field(None, description="ISO format date (YYYY-MM-DD) for CUSTOM end.")


class QueryFiltersSchema(BaseModel):
    service: Optional[str] = Field(None, description="Filter records by service name exactly.")
    region: Optional[str] = Field(None, description="Filter records by region name.")
    resource_id: Optional[str] = Field(None, description="Filter records by resource ID (OCID).")
    resource_name: Optional[str] = Field(None, description="Filter records by resource name.")
    anomaly_severity: Optional[str] = Field(None, description="Cost anomaly severity (LOW, MEDIUM, HIGH, CRITICAL).")
    anomaly_status: Optional[str] = Field(None, description="Cost anomaly status (OPEN, REVIEWED, DISMISSED).")
    waste_type: Optional[str] = Field(None, description="Waste finding type.")
    waste_confidence: Optional[str] = Field(None, description="Waste finding confidence (LOW, MEDIUM, HIGH).")
    waste_status: Optional[str] = Field(None, description="Waste finding status (OPEN, REVIEWED, DISMISSED).")


class ChatQueryPlan(BaseModel):
    intent: IntentEnum = Field(description="Detected user intent.")
    time_range: TimeRangeSchema = Field(description="Standardized time range details.")
    filters: QueryFiltersSchema = Field(default_factory=QueryFiltersSchema, description="Optional filters.")
    comparison_services: Optional[List[str]] = Field(None, description="Services to compare for COST_COMPARISON.")
    limit: Optional[int] = Field(5, description="Limit on results. Must be between 1 and 20.")


# --- STRUCTURED GROUNDING RESPONSE SCHEMA ---

class FinancialFactClaim(BaseModel):
    label: str = Field(description="Label of the value, e.g., 'Compute cost', 'Total cost', 'Savings delta'.")
    value: str = Field(description="Canonical string representation of the money amount, e.g. '120.00' or '0.00'.")
    currency: str = Field(description="Currency code, e.g., 'USD', 'UNKNOWN'.")


class SeverityClaim(BaseModel):
    resource: str = Field(description="Resource ID or service name.")
    severity: str = Field(description="Severity e.g. 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'.")


class ConfidenceClaim(BaseModel):
    resource: str = Field(description="Resource ID or service name.")
    confidence: str = Field(description="Confidence rating e.g. 'LOW', 'MEDIUM', 'HIGH'.")


class StructuredChatResponse(BaseModel):
    answer_text: str = Field(description="Natural language explanation of the query results. Must rely ONLY on provided deterministic context numbers.")
    referenced_financial_facts: List[FinancialFactClaim] = Field(default_factory=list, description="All financial claims made in answer_text.")
    referenced_severities: List[SeverityClaim] = Field(default_factory=list, description="All severities claimed in answer_text.")
    referenced_confidences: List[ConfidenceClaim] = Field(default_factory=list, description="All confidence ratings claimed in answer_text.")


# --- APPLICATION-LEVEL VALIDATOR ---

class QueryPlanValidator:
    @staticmethod
    def validate(plan: ChatQueryPlan) -> None:
        # 1. Enforce limit 1 <= limit <= 20
        if plan.limit is not None:
            if plan.limit < 1 or plan.limit > 20:
                raise ValidationError("Limit must be between 1 and 20.")
        else:
            plan.limit = 5

        # 2. Check Custom Date Range Rules
        if plan.time_range.type == TimeRangeTypeEnum.CUSTOM:
            start_str = plan.time_range.start_date
            end_str = plan.time_range.end_date
            if not start_str or not end_str:
                raise ValidationError("I couldn't use that date range. Please provide a valid start and end date.")
            
            date_regex = re.compile(r"^\d{4}-\d{2}-\d{2}$")
            if not date_regex.match(start_str) or not date_regex.match(end_str):
                raise ValidationError("I couldn't use that date range. Please provide a valid start and end date.")
            
            try:
                start_date = datetime.datetime.strptime(start_str, "%Y-%m-%d").date()
                end_date = datetime.datetime.strptime(end_str, "%Y-%m-%d").date()
            except ValueError:
                raise ValidationError("I couldn't use that date range. Please provide a valid start and end date.")
            
            if start_date > end_date:
                raise ValidationError("I couldn't use that date range. Please provide a valid start and end date.")

        # 3. Validate Severity Enum Values if provided
        if plan.filters.anomaly_severity is not None:
            sev = plan.filters.anomaly_severity.upper()
            if sev not in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
                raise ValidationError(f"Invalid anomaly severity: '{plan.filters.anomaly_severity}'")
            plan.filters.anomaly_severity = sev

        # Validate Anomaly Status Enum Values
        if plan.filters.anomaly_status is not None:
            stat = plan.filters.anomaly_status.upper()
            if stat not in ("OPEN", "REVIEWED", "DISMISSED"):
                raise ValidationError(f"Invalid anomaly status: '{plan.filters.anomaly_status}'")
            plan.filters.anomaly_status = stat

        # Validate Waste Confidence Enum Values
        if plan.filters.waste_confidence is not None:
            conf = plan.filters.waste_confidence.upper()
            if conf not in ("LOW", "MEDIUM", "HIGH"):
                raise ValidationError(f"Invalid waste confidence: '{plan.filters.waste_confidence}'")
            plan.filters.waste_confidence = conf

        # Validate Waste Status Enum Values
        if plan.filters.waste_status is not None:
            wstat = plan.filters.waste_status.upper()
            if wstat not in ("OPEN", "REVIEWED", "DISMISSED"):
                raise ValidationError(f"Invalid waste status: '{plan.filters.waste_status}'")
            plan.filters.waste_status = wstat

        # Validate Waste Type if provided
        if plan.filters.waste_type is not None:
            from analytics.models import WasteFinding
            valid_types = {k for k, _ in WasteFinding.WASTE_TYPE_CHOICES}
            wtype = plan.filters.waste_type.upper()
            if wtype not in valid_types:
                raise ValidationError(f"Invalid waste type: '{plan.filters.waste_type}'")
            plan.filters.waste_type = wtype

        # 4. Filter Compatibility Check
        filters = plan.filters
        intent = plan.intent

        # Helper to check if any filters are set except allowed ones
        all_filter_fields = {
            "service", "region", "resource_id", "resource_name",
            "anomaly_severity", "anomaly_status",
            "waste_type", "waste_confidence", "waste_status"
        }

        # Define allowed filters per intent
        allowed_filters = set()
        if intent == IntentEnum.TOTAL_COST:
            allowed_filters = {"service", "region"}
        elif intent == IntentEnum.HELP:
            allowed_filters = set()
        elif intent in (
            IntentEnum.SERVICE_COST,
            IntentEnum.REGION_COST,
            IntentEnum.TOP_SERVICES,
            IntentEnum.TOP_REGIONS,
            IntentEnum.COST_TREND,
            IntentEnum.COST_COMPARISON,
            IntentEnum.COST_INCREASE_EXPLANATION
        ):
            allowed_filters = {"service", "region"}
        elif intent in (IntentEnum.RESOURCE_COST, IntentEnum.TOP_RESOURCES):
            allowed_filters = {"service", "region", "resource_id", "resource_name"}
        elif intent == IntentEnum.ANOMALIES:
            allowed_filters = {"anomaly_severity", "anomaly_status"}
        elif intent in (IntentEnum.WASTE_FINDINGS, IntentEnum.POTENTIAL_SAVINGS):
            allowed_filters = {"waste_type", "waste_confidence", "waste_status"}

        # Check for any unsupported filters that are not None
        for field in all_filter_fields:
            if getattr(filters, field, None) is not None:
                if field not in allowed_filters:
                    raise ValidationError(f"Filter '{field}' is not supported for intent '{intent.value}'.")

        # 5. Validate and Clean comparison_services
        if intent != IntentEnum.COST_COMPARISON:
            if plan.comparison_services is not None:
                raise ValidationError("comparison_services is only allowed for COST_COMPARISON intent.")
        else:
            if plan.comparison_services is None:
                raise ValidationError("comparison_services is required for COST_COMPARISON.")
            
            sanitized = []
            for s in plan.comparison_services:
                if not isinstance(s, str):
                    raise ValidationError("Each service in comparison_services must be a string.")
                s_trimmed = s.strip()
                if not s_trimmed:
                    raise ValidationError("Comparison service cannot be empty or blank.")
                sanitized.append(s_trimmed)
            
            # Remove duplicates deterministically (preserves order)
            unique_svcs = []
            for s in sanitized:
                if s not in unique_svcs:
                    unique_svcs.append(s)
            
            if len(unique_svcs) < 2:
                raise ValidationError("At least 2 unique comparison services are required.")
            if len(unique_svcs) > 10:
                raise ValidationError("Cannot compare more than 10 services.")
            
            plan.comparison_services = unique_svcs
