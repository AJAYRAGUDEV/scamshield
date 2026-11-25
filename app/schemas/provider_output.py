"""The provider's output schema. Treated as UNTRUSTED input.

The provider returns EVIDENCE ONLY (see ``app/schemas/evidence.py``): scam-indicator signals plus a
coverage self-assessment. It does not return a score, a verdict, a confidence or a recommendation;
if it adds such fields they are ignored. ``ProviderAnalysis`` is the ``response_schema`` given to
Gemini and the validation model for whatever comes back.
"""
from __future__ import annotations

from app.schemas.evidence import EvidenceReport, EvidenceSignal

ProviderAnalysis = EvidenceReport
ProviderSignal = EvidenceSignal

__all__ = ["ProviderAnalysis", "ProviderSignal"]
