"""Provider interface. Services depend on this, never on Gemini types.

Implementations raise ``app.core.errors.AnalysisError`` (with a ``ReasonCode``) for every failure
that prevents a validated result, and return an already-validated ``ProviderAnalysis`` otherwise.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from app.schemas.provider_output import ProviderAnalysis


class AnalysisProvider(Protocol):
    async def analyze_text(self, text: str, url_facts: str | None = None) -> ProviderAnalysis:
        """Assess a message or call transcript. ``url_facts`` is optional server-computed link structure facts."""
        ...

    async def analyze_audio(self, path: Path) -> ProviderAnalysis:
        """Assess an audio file on local disk. The caller owns and deletes ``path``."""
        ...

    async def analyze_video(self, path: Path, on_stage: Callable[[str], None] | None = None) -> ProviderAnalysis:
        """Assess a video file on local disk (visual content, on-screen text and audio). The caller owns and deletes ``path``."""
        ...

    async def aclose(self) -> None:
        """Release network resources at application shutdown."""
        ...
