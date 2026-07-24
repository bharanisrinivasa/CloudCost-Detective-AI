import json
import logging
from django.conf import settings
from google import genai
from google.genai import types
from google.genai import errors

logger = logging.getLogger(__name__)

# --- CUSTOM EXCEPTIONS FOR DECOUPLING ---
class LLMException(Exception):
    """Base exception for all LLM provider errors."""
    pass

class LLMMissingAPIKeyError(LLMException):
    """Exception raised when the API key is not configured."""
    pass

class LLMTimeoutError(LLMException):
    """Exception raised when the LLM provider call times out."""
    pass

class LLMRateLimitError(LLMException):
    """Exception raised when hitting the provider's rate limit."""
    pass

class LLMInvalidResponseError(LLMException):
    """Exception raised when the response cannot be parsed or validated against the schema."""
    pass

class LLMProviderError(LLMException):
    """Generic exception for other provider-related errors."""
    pass


# --- PROVIDER ABSTRACT INTERFACE ---
class LLMProvider:
    def generate_explanation(self, system_prompt: str, user_prompt: str, response_schema=None) -> dict:
        """
        Sends the prompt to the LLM and returns the parsed JSON response.
        Should return a dictionary matching the response_schema structure.
        """
        raise NotImplementedError("Subclasses must implement generate_explanation")


# --- GEMINI PROVIDER IMPLEMENTATION ---
class GeminiProvider(LLMProvider):
    def __init__(self):
        self.api_key = getattr(settings, "GEMINI_API_KEY", "")
        self.model_name = getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")

        if not self.api_key:
            raise LLMMissingAPIKeyError("AI service is not configured.")

        try:
            self.client = genai.Client(api_key=self.api_key)
        except Exception as e:
            logger.error("Failed to initialize Gemini Client: %s", type(e).__name__)
            raise LLMProviderError("AI explanation generation is temporarily unavailable.")

    def generate_explanation(self, system_prompt: str, user_prompt: str, response_schema=None) -> dict:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.0,  # High precision
        )
        if response_schema:
            config.response_mime_type = "application/json"
            config.response_schema = response_schema

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=user_prompt,
                config=config
            )
        except errors.APIError as e:
            # Map known Gemini SDK errors
            logger.error("Gemini API request failed: Code %s, %s", e.code, type(e).__name__)
            if e.code == 429:
                raise LLMRateLimitError("AI explanation service is temporarily busy. Please try again later.")
            elif e.code in (401, 403):
                raise LLMMissingAPIKeyError("AI service is not configured.")
            elif e.code in (408, 504):
                raise LLMTimeoutError("AI explanation generation timed out. Please try again.")
            else:
                raise LLMProviderError("AI explanation generation is temporarily unavailable.")
        except Exception as e:
            # Catch other general exceptions (network connection, timeout, etc.)
            err_str = str(e).lower()
            logger.error("General LLM Provider Exception: %s", type(e).__name__)
            if "timeout" in err_str or "timed out" in err_str:
                raise LLMTimeoutError("AI explanation generation timed out. Please try again.")
            else:
                raise LLMProviderError("AI explanation generation is temporarily unavailable.")

        # Handle parsed structured output
        if response_schema:
            if hasattr(response, "parsed") and response.parsed is not None:
                try:
                    # google-genai returns a Pydantic object if response_schema is passed
                    return response.parsed.model_dump()
                except Exception as pe:
                    logger.error("Failed to dump Pydantic response: %s", type(pe).__name__)
                    raise LLMInvalidResponseError("The AI provider returned an invalid explanation.")
            else:
                logger.error("Structured response requested but response.parsed is missing or None.")
                raise LLMInvalidResponseError("The AI provider returned an invalid explanation.")

        # Fallback to loading text as JSON only when response_schema is None
        if not response.text:
            raise LLMInvalidResponseError("The AI provider returned an empty response.")

        try:
            return json.loads(response.text)
        except json.JSONDecodeError as je:
            logger.error("JSON Decode Error for Gemini response text: %s", type(je).__name__)
            raise LLMInvalidResponseError("The AI provider returned an invalid explanation.")
