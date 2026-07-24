from pydantic import BaseModel, Field
from typing import List


class AIExplanationResponseSchema(BaseModel):
    summary: str = Field(description="A concise summary of the cost finding in 1-2 sentences.")
    why_flagged: str = Field(description="Explanation of why this resource/date was flagged based strictly on the deterministic evidence.")
    evidence: List[str] = Field(description="List of specific, concrete bullet points citing numeric figures or dates from the evidence supporting the finding.")
    financial_impact: str = Field(description="Faithful description of the cost anomaly severity or estimated monthly waste savings, referencing the deterministic numbers exactly.")
    confidence_explanation: str = Field(description="Explanation of the confidence rating (LOW, MEDIUM, HIGH) or severity (LOW, MEDIUM, HIGH, CRITICAL) using only the provided facts.")
    recommended_next_step: str = Field(description="Safe, non-destructive investigation guidance (e.g., inspect in console, review historical usage). No destructive commands.")
    limitations: str = Field(description="Limitations note explaining that this is generated from historical billing cost patterns and does not confirm actual runtime/utilization state.")
