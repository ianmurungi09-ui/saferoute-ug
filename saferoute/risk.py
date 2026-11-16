"""Illustrative risk tiering for the insurer / SACCO integration hook.

The factors are placeholders that show how compliance and driving data could
combine. Real premiums are set by underwriters, and sharing driver data with an
insurer needs the driver's consent."""
from __future__ import annotations

_COMPLIANCE_FACTOR = {"COMPLIANT": 1.0, "CONDITIONAL": 1.15, "UNVERIFIED": 1.3, "EXPIRED": 1.4, "DMC": 2.0}


def _driver_factor(score: float | None) -> float:
    if score is None:
        return 1.0
    for floor, f in ((90, 0.9), (80, 1.0), (70, 1.1), (50, 1.25)):
        if score >= floor:
            return f
    return 1.5


def risk_profile(compliance_status: str, score: float | None) -> dict:
    factor = round(_COMPLIANCE_FACTOR.get(compliance_status, 1.3) * _driver_factor(score), 2)
    tier = "A" if factor <= 0.95 else "B" if factor <= 1.1 else "C" if factor <= 1.4 else "D"
    return {"risk_tier": tier, "indicative_premium_factor": factor,
            "basis": {"compliance": compliance_status, "driving_score": score}}
