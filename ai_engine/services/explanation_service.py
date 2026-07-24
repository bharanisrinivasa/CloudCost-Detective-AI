import hashlib
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from django.conf import settings
from ai_engine.models import AIExplanation
from ai_engine.services.provider import (
    GeminiProvider,
    LLMMissingAPIKeyError,
    LLMTimeoutError,
    LLMRateLimitError,
    LLMInvalidResponseError,
    LLMProviderError,
)
from ai_engine.services.prompt_builder import build_system_prompt, build_user_prompt, PROMPT_VERSION
from ai_engine.services.schemas import AIExplanationResponseSchema

logger = logging.getLogger(__name__)

# --- HELPERS FOR GROUNDING VALIDATION ---
def extract_number_near_keyword(text: str, keyword: str, window: int = 20):
    """
    Finds all numbers near occurrences of keyword in the text corpus
    without slicing, only considering numbers that appear after the keyword.
    Returns Decimal objects for monetary safety.
    """
    text = text.lower()
    keyword = keyword.lower()
    keyword_indices = [m.start() for m in re.finditer(re.escape(keyword), text)]
    if not keyword_indices:
        return

    for match in re.finditer(r'\b\d+(?:\.\d+)?\b', text):
        try:
            num_val = Decimal(match.group())
        except (ValueError, InvalidOperation):
            continue
        num_start = match.start()

        for kw_idx in keyword_indices:
            if num_start < kw_idx:
                # Ignore numbers appearing before the keyword
                continue
            dist = num_start - (kw_idx + len(keyword))

            if dist <= window:
                yield num_val
                break


def validate_grounded_response(source, response_dict: dict) -> tuple[bool, str]:
    """
    Validates that the AI explanation does not contradict deterministic facts
    from the source finding (severity, confidence, costs, savings) using Decimal comparison.
    """
    texts = [
        response_dict.get("summary", ""),
        response_dict.get("why_flagged", ""),
        " ".join(response_dict.get("evidence", [])),
        response_dict.get("financial_impact", ""),
        response_dict.get("confidence_explanation", ""),
        response_dict.get("recommended_next_step", ""),
        response_dict.get("limitations", "")
    ]
    corpus = " ".join(texts).lower()

    source_class = source.__class__.__name__

    if source_class == "CostAnomaly":
        # 1. Verify severity is not contradicted
        current_severity = source.severity.lower()
        all_severities = ["low", "medium", "high", "critical"]
        for sev in all_severities:
            if sev != current_severity:
                patterns = [
                    rf"\b{sev}\s+severity\b",
                    rf"\bseverity\s+(?:is|of|level|classification)?\s*{sev}\b",
                    rf"\bseverity\s*:\s*{sev}\b",
                ]
                for pat in patterns:
                    if re.search(pat, corpus):
                        return False, "INVALID_RESPONSE"

        # 2. Verify actual cost is not contradicted in text near cost keywords
        actual_val = Decimal(source.actual_cost)
        for num in extract_number_near_keyword(corpus, "actual"):
            if num != actual_val:
                # Ensure we don't confuse actual cost with expected cost or deviation
                if num not in (Decimal(source.expected_cost), Decimal(source.deviation_percentage)):
                    return False, "INVALID_RESPONSE"

        # 3. Verify expected cost is not contradicted
        expected_val = Decimal(source.expected_cost)
        for num in extract_number_near_keyword(corpus, "expected"):
            if num != expected_val:
                if num not in (Decimal(source.actual_cost), Decimal(source.deviation_percentage)):
                    return False, "INVALID_RESPONSE"

        # 4. Verify deviation percentage is not contradicted
        dev_val = Decimal(source.deviation_percentage)
        for num in extract_number_near_keyword(corpus, "deviation"):
            if num != dev_val:
                if num not in (Decimal(source.actual_cost), Decimal(source.expected_cost)):
                    return False, "INVALID_RESPONSE"

        # 5. Verify z_score is not contradicted
        if source.z_score is not None:
            z_val = Decimal(str(source.z_score))
            for num in extract_number_near_keyword(corpus, "z-score") or extract_number_near_keyword(corpus, "z_score"):
                if num != z_val:
                    return False, "INVALID_RESPONSE"

    elif source_class == "WasteFinding":
        # 1. Verify confidence is not contradicted
        current_conf = source.confidence.lower()
        all_confs = ["low", "medium", "high"]
        for conf in all_confs:
            if conf != current_conf:
                patterns = [
                    rf"\b{conf}\s+confidence\b",
                    rf"\bconfidence\s+(?:is|of|level|rating)?\s*{conf}\b",
                    rf"\bconfidence\s*:\s*{conf}\b"
                ]
                for pat in patterns:
                    if re.search(pat, corpus):
                        return False, "INVALID_RESPONSE"

        # 2. Verify savings are not contradicted
        savings_val = Decimal(source.estimated_monthly_savings)
        for num in extract_number_near_keyword(corpus, "save"):
            if num != savings_val:
                if num not in (Decimal(source.total_cost), Decimal(source.estimated_monthly_cost)):
                    return False, "INVALID_RESPONSE"

        # 3. Verify monthly cost is not contradicted
        monthly_cost_val = Decimal(source.estimated_monthly_cost)
        for num in extract_number_near_keyword(corpus, "monthly cost") or extract_number_near_keyword(corpus, "run rate"):
            if num != monthly_cost_val:
                if num not in (Decimal(source.total_cost), Decimal(source.estimated_monthly_savings)):
                    return False, "INVALID_RESPONSE"

    return True, ""


# --- DETERMINISTIC DATA EXTRACTORS ---
def get_anomaly_deterministic_data(source) -> dict:
    """
    Extracts the specific deterministic fields for CostAnomaly.
    These fields participate in input_hash and prompt generation.
    Monetary values are represented as canonical strings for Decimal precision.
    """
    return {
        "anomaly_type": source.anomaly_type,
        "detected_date": source.detected_date.isoformat(),
        "service_name": source.service_name or "",
        "resource_id": source.resource_id or "",
        "resource_name": source.resource_name or "",
        "region": source.region or "",
        "actual_cost": str(Decimal(source.actual_cost).quantize(Decimal("0.01"))),
        "expected_cost": str(Decimal(source.expected_cost).quantize(Decimal("0.01"))),
        "deviation_percentage": str(Decimal(source.deviation_percentage).quantize(Decimal("0.01"))),
        "z_score": float(source.z_score) if source.z_score is not None else None,
        "severity": source.severity,
        "description": source.description or "",
    }


def get_waste_deterministic_data(source) -> dict:
    """
    Extracts the specific deterministic fields for WasteFinding.
    These fields participate in input_hash and prompt generation.
    Monetary values are represented as canonical strings for Decimal precision.
    """
    return {
        "waste_type": source.waste_type,
        "resource_id": source.resource_id or "",
        "resource_name": source.resource_name or "",
        "service_name": source.service_name,
        "region": source.region or "",
        "currency": source.currency,
        "first_seen": source.first_seen.isoformat(),
        "last_seen": source.last_seen.isoformat(),
        "observation_days": int(source.observation_days),
        "calendar_span_days": int(source.calendar_span_days),
        "coverage_ratio": float(source.coverage_ratio),
        "total_cost": str(Decimal(source.total_cost).quantize(Decimal("0.01"))),
        "average_daily_cost": str(Decimal(source.average_daily_cost).quantize(Decimal("0.01"))),
        "estimated_monthly_cost": str(Decimal(source.estimated_monthly_cost).quantize(Decimal("0.01"))),
        "estimated_monthly_savings": str(Decimal(source.estimated_monthly_savings).quantize(Decimal("0.01"))),
        "confidence": source.confidence,
        "evidence": source.evidence or "",
    }


def calculate_input_hash(finding_data: dict) -> str:
    """Computes a deterministic hash of the finding's input data."""
    serialized = json.dumps(finding_data, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# --- SERVICE ENTRY POINT ---
def get_or_generate_explanation(user, source, force_regenerate: bool = False) -> AIExplanation:
    """
    Coordinates explanation generation.
    1. Computes the deterministic hash of the finding.
    2. Checks for an existing explanation record.
    3. If not stale and force_regenerate is False, returns the cached explanation.
    4. Otherwise, requests a new structured explanation from the provider.
    5. Performs grounding validation.
    6. Stores/updates the result in the database.
    """
    source_class = source.__class__.__name__
    if source_class == "CostAnomaly":
        source_type = "ANOMALY"
        finding_data = get_anomaly_deterministic_data(source)
    elif source_class == "WasteFinding":
        source_type = "WASTE"
        finding_data = get_waste_deterministic_data(source)
    else:
        raise ValueError(f"Unsupported explanation source type: {source_class}")

    current_hash = calculate_input_hash(finding_data)

    # Scoping: query by user + source_type + source_id
    explanation_record = AIExplanation.objects.filter(
        user=user,
        source_type=source_type,
        source_id=source.pk
    ).first()

    # Determine if we can use the cached record
    if explanation_record and explanation_record.status == "GENERATED" and not force_regenerate:
        if explanation_record.input_hash == current_hash:
            logger.info("Using cached valid explanation for %s ID %s", source_type, source.pk)
            return explanation_record

    # Create placeholder record if it doesn't exist
    if not explanation_record:
        explanation_record = AIExplanation(
            user=user,
            source_type=source_type,
            source_id=source.pk,
            prompt_version=PROMPT_VERSION,
            model_name=getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")
        )

    explanation_record.input_hash = current_hash
    explanation_record.prompt_version = PROMPT_VERSION
    explanation_record.model_name = getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")

    # Generate prompts
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(source_type, finding_data)

    try:
        provider = GeminiProvider()
        response_dict = provider.generate_explanation(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=AIExplanationResponseSchema
        )

        # Re-validate JSON using the Pydantic schema class directly
        validated_schema = AIExplanationResponseSchema(**response_dict)
        validated_dict = validated_schema.model_dump()

        # Perform Grounding Validation
        is_grounded, err_code = validate_grounded_response(source, validated_dict)
        if not is_grounded:
            raise LLMInvalidResponseError(err_code)

        # Update record with successful structured output
        explanation_record.summary = validated_dict["summary"]
        explanation_record.why_flagged = validated_dict["why_flagged"]
        explanation_record.evidence_summary = "\n".join(validated_dict["evidence"])
        explanation_record.financial_impact = validated_dict["financial_impact"]
        explanation_record.confidence_explanation = validated_dict["confidence_explanation"]
        explanation_record.recommended_next_step = validated_dict["recommended_next_step"]
        explanation_record.limitations = validated_dict["limitations"]
        explanation_record.status = "GENERATED"
        explanation_record.error_message = ""
        explanation_record.save()

    except LLMMissingAPIKeyError as e:
        explanation_record.status = "FAILED"
        explanation_record.error_message = "MISSING_API_KEY"
        explanation_record.save()
        raise e
    except LLMTimeoutError as e:
        explanation_record.status = "FAILED"
        explanation_record.error_message = "TIMEOUT"
        explanation_record.save()
        raise e
    except LLMRateLimitError as e:
        explanation_record.status = "FAILED"
        explanation_record.error_message = "RATE_LIMIT"
        explanation_record.save()
        raise e
    except LLMInvalidResponseError:
        explanation_record.status = "FAILED"
        explanation_record.error_message = "INVALID_RESPONSE"
        explanation_record.save()
        raise
    except Exception as e:
        logger.error("Unexpected error in explanation generation service: %s", type(e).__name__)
        explanation_record.status = "FAILED"
        explanation_record.error_message = "PROVIDER_ERROR"
        explanation_record.save()
        raise LLMProviderError("AI explanation generation is temporarily unavailable.")

    return explanation_record


def get_or_generate_recommendation_explanation(user, rec, force_regenerate: bool = False) -> dict:
    """
    Coordinates explanation generation for recommendations.
    Uses structured responses from the Gemini API and caching mechanisms.
    """
    from analytics.services.recommendation_engine import generate_explanation_hash
    current_hash = generate_explanation_hash(rec)
    
    if rec.ai_explanation_json and rec.ai_explanation_hash == current_hash and not force_regenerate:
        logger.info("Using cached valid explanation for Recommendation ID %s", rec.pk)
        return rec.ai_explanation_json

    # Prepare serialized data for Gemini
    rec_data = {
        "recommendation_type": rec.recommendation_type,
        "recommendation_scope": rec.recommendation_scope,
        "resource_id": rec.resource_id or "",
        "resource_name": rec.resource_name or "",
        "identity_type": rec.identity_type,
        "identity_value": rec.identity_value,
        "service_name": rec.service_name,
        "region": rec.region,
        "current_monthly_cost": str(rec.current_monthly_cost) if rec.current_monthly_cost is not None else None,
        "estimated_monthly_savings": str(rec.estimated_monthly_savings) if rec.estimated_monthly_savings is not None else None,
        "currency": rec.currency,
        "confidence": rec.confidence,
        "priority": rec.priority,
        "evidence": rec.evidence or "",
        "recommended_action": rec.recommended_action or "",
        "limitations": rec.limitations or "",
    }
    
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt("RECOMMENDATION", rec_data)
    
    try:
        provider = GeminiProvider()
        response_dict = provider.generate_explanation(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=AIExplanationResponseSchema
        )
        
        validated_schema = AIExplanationResponseSchema(**response_dict)
        validated_dict = validated_schema.model_dump()
        
        # Grounding check: verify that costs and savings are not contradicted in text near keywords
        corpus = " ".join([
            validated_dict["summary"],
            validated_dict["why_flagged"],
            " ".join(validated_dict["evidence"]),
            validated_dict["financial_impact"],
            validated_dict["confidence_explanation"],
            validated_dict["recommended_next_step"],
            validated_dict["limitations"]
        ]).lower()
        
        # Check cost
        if rec.current_monthly_cost is not None:
            cost_val = Decimal(rec.current_monthly_cost)
            for num in extract_number_near_keyword(corpus, "cost") or extract_number_near_keyword(corpus, "monthly"):
                if num != cost_val:
                    if rec.estimated_monthly_savings is not None and num == Decimal(rec.estimated_monthly_savings):
                        continue
                    raise LLMInvalidResponseError("INVALID_RESPONSE")
                    
        # Check savings
        if rec.estimated_monthly_savings is not None:
            savings_val = Decimal(rec.estimated_monthly_savings)
            for num in extract_number_near_keyword(corpus, "save") or extract_number_near_keyword(corpus, "savings"):
                if num != savings_val:
                    if rec.current_monthly_cost is not None and num == Decimal(rec.current_monthly_cost):
                        continue
                    raise LLMInvalidResponseError("INVALID_RESPONSE")
        
        # Cache successful explanation
        rec.ai_explanation_json = validated_dict
        rec.ai_explanation_hash = current_hash
        rec.save()
        return validated_dict

    except LLMMissingAPIKeyError as e:
        raise e
    except LLMTimeoutError as e:
        raise e
    except LLMRateLimitError as e:
        raise e
    except LLMInvalidResponseError:
        raise
    except Exception as e:
        logger.error("Unexpected error in recommendation explanation generation service: %s", type(e).__name__)
        raise LLMProviderError("AI explanation generation is temporarily unavailable.")

