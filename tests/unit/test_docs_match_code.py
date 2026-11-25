"""docs/risk-engine.md must not drift from the code it documents."""
from __future__ import annotations

import re
from pathlib import Path

from app.core.errors import ReasonCode
from app.services import risk_engine as engine

DOC = (Path(__file__).resolve().parents[2] / "docs" / "risk-engine.md").read_text(encoding="utf-8")


def test_weight_table_matches_the_code():
    rows = dict(re.findall(r"^\| `([a-z_]+)` \| (0\.\d\d) \|", DOC, re.M))
    assert {k: float(v) for k, v in rows.items()} == engine.CATEGORY_WEIGHTS


def test_factors_and_constants_are_documented_correctly():
    for name, value in {**engine.SEVERITY_FACTORS, **engine.RELIABILITY_FACTORS}.items():
        assert re.search(rf"`{name}` {value:.2f}", DOC), name
    assert f"within {int(engine.EVENT_WINDOW_SECONDS)} seconds" in DOC and engine.CONFIDENCE_CAP == 0.95 and "0.95" in DOC


def test_thresholds_and_reason_codes_are_documented():
    assert "| 0-24 | `low_risk` |" in DOC and "| 25-59 | `suspicious` |" in DOC and "| 60-100 | `high_risk` |" in DOC
    for reason in ReasonCode:
        assert f"`{reason.value}`" in DOC, reason


def test_the_required_disclaimers_are_present():
    flat = " ".join(DOC.replace("> ", " ").split())
    assert "A *Low Risk* result does **not** prove that content, a person or a communication is authentic or safe" in flat
    assert "does not guarantee that scams are detected" in flat
    assert "does not prove that audio or video is synthetic or manipulated" in flat


def test_the_scaling_description_matches_the_constants():
    flat = " ".join(DOC.replace("> ", " ").split())
    assert f"{engine.REPEAT_CAP_MULTIPLIER} × CATEGORY_WEIGHT" in flat and f"{engine.DECAY}^k" in flat


# ------------------------------------------------------------------ Phase 5 security documentation
SECURITY = (Path(__file__).resolve().parents[2] / "docs" / "security.md").read_text(encoding="utf-8")
README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")


def test_security_doc_states_it_is_not_a_production_ready_claim():
    flat = " ".join(SECURITY.split())
    assert "not a production-ready or \"completely secure\" system" in flat
    assert "Production deployment requirements" in flat and "Known limitations" in flat


def test_security_doc_matches_the_security_code():
    from app.core import security
    from app.core.config import Settings

    s = Settings()
    for name, default in (("RATE_LIMIT_TEXT", s.rate_limit_text), ("RATE_LIMIT_AUDIO", s.rate_limit_audio), ("RATE_LIMIT_VIDEO", s.rate_limit_video),
                          ("RATE_LIMIT_GLOBAL", s.rate_limit_global), ("RATE_LIMIT_DEFAULT", s.rate_limit_default)):
        assert re.search(rf"`{name}` \| {re.escape(default.replace('/minute', '/minute'))}", SECURITY), name
    assert f"{security.MULTIPART_OVERHEAD_BYTES // 1024} KiB" in SECURITY and f"{security.DEFAULT_BODY_LIMIT_BYTES // 1024} KiB" in SECURITY
    for code in ("INVALID_INPUT", "PAYLOAD_TOO_LARGE", "UNSUPPORTED_MEDIA_TYPE", "INVALID_REQUEST", "RATE_LIMITED", "SERVICE_BUSY"):
        assert code in SECURITY
    for directive in security.CONTENT_SECURITY_POLICY.split("; "):
        assert directive.split()[0] in SECURITY, directive


def test_readme_documents_every_environment_variable():
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    for name in re.findall(r"^([A-Z_]+)=", example, re.M):
        assert f"`{name}`" in README, name


# ------------------------------------------------------------------ Phase 6 frontend documentation
FRONTEND_DOC = (Path(__file__).resolve().parents[2] / "docs" / "frontend.md").read_text(encoding="utf-8")


def test_frontend_doc_lists_every_module_and_no_ghost_modules():
    js_dir = Path(__file__).resolve().parents[2] / "frontend" / "js"
    on_disk = {p.name for p in js_dir.glob("*.js")}
    documented = set(re.findall(r"^    ([\w-]+\.js)\b", FRONTEND_DOC, re.M))
    assert documented == on_disk


def test_frontend_doc_names_the_cdn_versions_the_page_loads():
    html = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")
    for version in re.findall(r'<script[^>]+src="https://[^"]*?(tailwindcss\.com/[\d.]+|chart\.js@[\d.]+|lucide@[\d.]+)', html):
        assert version in FRONTEND_DOC, version
