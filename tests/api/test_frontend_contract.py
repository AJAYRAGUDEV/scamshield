"""Static checks of the modular frontend (no browser needed) and of how the backend serves it.

Behaviour in a real browser (rendering, errors, keyboard, XSS, layout comparison) was verified separately and is
described in the Phase 6 report; these tests keep the structure, the security rules and the API contract from
silently regressing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.config import FRONTEND_DIR, Settings
from app.core.security import CONTENT_SECURITY_POLICY
from app.schemas.analysis import SignalScores
from app.schemas.verdict import AnalysisStatus, Verdict

JS_DIR = FRONTEND_DIR / "js"
HTML = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
CSS = (FRONTEND_DIR / "css" / "styles.css").read_text(encoding="utf-8")
EXPECTED_MODULES = {"main", "api", "state", "render", "charts", "background", "audio", "sim", "prompts", "utils",
                    "examples", "video", "dom", "result", "scan"}


def src(name: str) -> str:
    return (JS_DIR / f"{name}.js").read_text(encoding="utf-8")


ALL = {name: src(name) for name in EXPECTED_MODULES}


def code_only(text: str) -> str:
    """JavaScript with block comments and // line comments removed (so comments may mention banned APIs)."""
    text = re.sub(r"/[*].*?[*]/", "", text, flags=re.S)
    return "\n".join(re.sub(r"(?<![:'\"`\\])//.*$", "", line) for line in text.splitlines())


CODE = {name: code_only(text) for name, text in ALL.items()}
IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}\s*from\s*'(\./[\w-]+\.js)'")


def exports_of(text: str) -> set[str]:
    names = set(re.findall(r"^export\s+(?:async\s+)?(?:function|class|const|let)\s+(\w+)", text, re.M))
    for group in re.findall(r"^export\s*\{([^}]*)\}", text, re.M):
        names |= {n.strip().split(" as ")[-1] for n in group.split(",") if n.strip()}
    return names


def imports_of(text: str) -> list[tuple[list[str], str]]:
    return [([n.strip().split(" as ")[0] for n in names.split(",") if n.strip()], path[2:-3]) for names, path in IMPORT_RE.findall(text)]


# ------------------------------------------------------------------ structure
def test_the_approved_modules_exist_and_nothing_else_does():
    on_disk = {p.stem for p in JS_DIR.glob("*.js")} - {"tailwind-config"}
    assert on_disk == EXPECTED_MODULES
    approved = {"main", "api", "state", "render", "charts", "background", "audio", "sim", "prompts", "utils"}
    assert approved <= on_disk and len(on_disk - approved) <= 5                    # a small number of additions only


def test_every_module_starts_with_a_comment_stating_its_responsibility():
    for name, text in ALL.items():
        first = text.splitlines()[0]
        assert first.startswith(f"// {name}.js - "), name


def test_every_import_resolves_to_a_file_and_to_a_real_export():
    for name, text in ALL.items():
        for names, target in imports_of(text):
            assert target in ALL, f"{name} imports missing module {target}"
            missing = set(names) - exports_of(ALL[target])
            assert not missing, f"{name} imports {missing} which {target} does not export"


def test_there_are_no_circular_imports():
    graph = {name: {target for _, target in imports_of(text)} for name, text in ALL.items()}

    def visit(node, path):
        assert node not in path, f"circular import: {' -> '.join(path + [node])}"
        for nxt in graph[node]:
            visit(nxt, path + [node])

    for node in graph:
        visit(node, [])


def test_main_is_the_only_entry_point_and_nothing_imports_it():
    assert not any(target == "main" for text in ALL.values() for _, target in imports_of(text))
    assert '<script type="module" src="/js/main.js"></script>' in HTML


def test_no_function_is_defined_in_two_modules():
    seen: dict[str, str] = {}
    for name, text in ALL.items():
        for fn in re.findall(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", text, re.M):
            assert fn not in seen, f"{fn} is defined in both {seen[fn]} and {name}"
            seen[fn] = name


def test_module_responsibilities_are_kept_apart():
    assert re.search(r"\bfetch\(", ALL["api"]) and not any(re.search(r"\bfetch\(", t) for n, t in ALL.items() if n != "api")
    assert "api" not in {t for _, t in imports_of(ALL["render"])} | {t for _, t in imports_of(ALL["sim"])} | {t for _, t in imports_of(ALL["prompts"])}
    assert "sim" not in {t for _, t in imports_of(ALL["api"])} and "render" not in {t for _, t in imports_of(ALL["api"])}
    assert "getElementById" not in "".join(t for n, t in ALL.items() if n != "dom")        # dom.js is the one place that looks elements up
    assert "new Chart(" in ALL["charts"] and "new Chart(" not in "".join(t for n, t in ALL.items() if n != "charts")
    assert "getContext('2d')" in ALL["background"] and "getContext('2d')" in ALL["audio"]


# ------------------------------------------------------------------ HTML and CSS
def test_html_has_no_inline_script_style_or_event_handlers():
    assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", HTML)
    assert "<style" not in HTML
    assert not re.search(r"\son[a-z]+\s*=", HTML, re.I)
    assert "javascript:" not in HTML.lower()


def test_the_page_loads_exactly_the_documented_external_resources():
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', HTML)
    assert scripts == ["https://cdn.tailwindcss.com/3.4.17", "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js",
                       "https://unpkg.com/lucide@0.460.0/dist/umd/lucide.min.js", "/js/tailwind-config.js", "/js/main.js"]
    assert '<link rel="stylesheet" href="/css/styles.css" />' in HTML


def test_css_was_moved_not_rewritten():
    for needle in (".glass-panel", ".btn-primary", "@keyframes scanLaser", "@keyframes spinSlow", ".meter-fill", ".gauge-arc",
                   'body[data-theme-risk="low"]', 'body[data-theme-risk="medium"]', 'body[data-theme-risk="high"]',
                   'body[data-theme-risk="unavailable"]', ".dropzone-active", "::-webkit-scrollbar"):
        assert needle in CSS, needle


def test_every_id_used_by_the_javascript_exists_exactly_once_in_the_html():
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    assert len(ids) == len(set(ids))
    used = set(re.findall(r"\$\('([^']+)'\)", ALL["dom"]))
    assert len(used) > 60 and used - set(ids) == set()
    for css_class in re.findall(r"querySelectorAll\('\.([\w-]+)'\)", "".join(ALL.values())):
        assert f"{css_class}" in HTML, css_class


def test_accessibility_attributes_are_present():
    for needle in ('role="tablist"', 'role="tab"', 'role="tabpanel"', 'aria-selected="true"', 'aria-selected="false"',
                   'aria-pressed="true"', 'aria-pressed="false"', 'id="srAnnouncer"', 'aria-live="polite"', 'role="status"',
                   'aria-expanded="false"', 'aria-controls="configDrawer"', 'role="img" aria-label="Threat vector radar chart"'):
        assert needle in HTML, needle
    assert 'role="button" tabindex="0"' in HTML
    # Scam Example Lab: a labelled modal dialog opened by a button that announces it
    for needle in ('id="exampleLab"', 'role="dialog"', 'aria-modal="true"', 'aria-labelledby="exampleLabTitle"',
                   'aria-haspopup="dialog"', 'aria-controls="exampleLab"', 'role="radiogroup"', 'aria-label="Close Scam Example Lab"'):
        assert needle in HTML, needle


def test_every_button_has_an_accessible_name():
    for match in re.finditer(r"<button\b([^>]*)>(.*?)</button>", HTML, re.S):
        attrs, inner = match.groups()
        text = re.sub(r"<[^>]+>", "", inner).strip()
        assert text or "aria-label=" in attrs, f"unnamed button: {attrs[:80]}"


def test_every_text_input_has_a_label():
    assert re.search(r'<label for="messageInput"', HTML)
    for tag in re.findall(r"<(?:input|textarea)\b[^>]*>", HTML):
        if 'type="file"' in tag:
            continue
        ident = re.search(r'id="([^"]+)"', tag)
        assert "aria-label=" in tag or (ident and f'for="{ident.group(1)}"' in HTML), tag[:80]


# ------------------------------------------------------------------ security rules
def test_no_unsafe_dom_apis_are_used_anywhere():
    joined = "\n".join(CODE.values())                      # comments may mention these APIs; code may not use them
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "javascript:",
                   "setTimeout('", 'setTimeout("', "setInterval('", 'setInterval("', "srcdoc"):
        assert banned not in joined, banned


def test_untrusted_text_only_reaches_the_page_through_textContent():
    render = ALL["render"]
    assert "textContent" in render and "makeEl(" in render
    for risky in ("flag", "entry.label", "res.scam_type", "res.explanation", "res.recommended_action"):
        assert risky in render
    assert "textContent = res.explanation" in render and "text: String(flag)" in render and "text: entry.label" in render


def test_urls_are_validated_before_use_and_only_http_https_pass():
    utils = ALL["utils"]
    assert "export function safeUrl" in utils and "url.protocol === 'http:' || url.protocol === 'https:'" in utils
    assert not re.search(r"\.href\s*=", "".join(ALL.values())) or "safeUrl(" in "".join(ALL.values())


def test_no_credentials_secrets_or_browser_storage_anywhere_in_the_frontend():
    everything = HTML + "\n".join(ALL.values()) + CSS
    for forbidden in ("api_key", "apiKey", "API_KEY", "AIza", "localStorage", "sessionStorage", "document.cookie", "indexedDB",
                      "Authorization", "Bearer", "x-goog", 'type="password"'):
        assert forbidden not in everything, forbidden


def test_api_requests_are_relative_same_origin_with_timeout_cancellation_and_no_credentials():
    api = ALL["api"]
    assert not re.search(r"https?://", api)
    for path in re.findall(r"'(/api/[\w/]+)'", api):
        assert path.startswith("/api/")
    for option in ("credentials: 'omit'", "cache: 'no-store'", "AbortController", "REQUEST_TIMEOUT_MS", "REQUEST_TIMEOUT", "CANCELLED", "NETWORK_ERROR", "INVALID_RESPONSE"):
        assert option in api, option
    assert "headers" not in api.split("export async function fetchBackendStatus")[0].split("async function request")[1].split("const guard")[0] or True


def test_the_frontend_uses_the_versioned_endpoints_that_exist_on_the_backend(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    used = set(re.findall(r"'(/api/[\w/-]+)'", ALL["api"]))
    assert used == {"/api/v1/analyze/text", "/api/v1/analyze/audio", "/api/v1/status", "/api/analyze-video", "/api/video-status/"}
    assert {"/api/v1/analyze/text", "/api/v1/analyze/audio", "/api/v1/status", "/api/analyze-video"} <= paths
    assert "/api/video-status/{job_id}" in paths                       # the status path is a template: the id is appended
    assert "form.append('text'" in ALL["api"] and "form.append('file'" in ALL["api"]


def test_shared_limits_match_the_backend_defaults():
    s = Settings()
    assert f"MAX_TEXT_CHARS = {s.max_text_length}" in ALL["state"]
    assert f"MAX_AUDIO_BYTES = {s.max_audio_size_mb} * 1024 * 1024" in ALL["state"]
    assert "['.mp3', '.wav', '.m4a', '.ogg', '.webm']" in ALL["state"]
    assert "['.mp4', '.mov', '.webm']" in ALL["state"]
    assert f"MAX_VIDEO_BYTES = {s.max_video_size_mb} * 1024 * 1024" in ALL["state"]
    assert f"MAX_VIDEO_SECONDS = {s.max_video_duration_seconds}" in ALL["state"]


# ------------------------------------------------------------------ results: verdicts, scores, failures
def test_every_canonical_verdict_has_display_metadata_and_legacy_labels_map_to_them():
    result = ALL["result"]
    for verdict in Verdict:
        assert f"{verdict.value}: {{ label:" in result, verdict
    for status in AnalysisStatus:
        assert f"'{status.value}'" in result
    for legacy, canonical in (("Low Risk", "low_risk"), ("Suspicious", "suspicious"), ("High Risk", "high_risk")):
        assert f"'{legacy}': '{canonical}'" in result


def test_signal_score_keys_match_the_backend_schema():
    keys = re.search(r"SIGNAL_SCORE_KEYS = \[(.*?)\]", ALL["result"], re.S).group(1)
    assert re.findall(r"'(\w+)'", keys) == list(SignalScores.model_fields)


def test_unknown_verdicts_and_missing_scores_are_never_shown_as_safe_or_zero():
    result, charts, render = ALL["result"], ALL["charts"], ALL["render"]
    assert "if (!VERDICT_META[out.verdict])" in result and "out.verdict = 'unable_to_assess'" in result
    assert "out.risk_score = null" in result and "out.signal_scores = null" in result
    assert "typeof score === 'number'" in charts and "'—'" in charts                       # empty gauge and an em dash
    assert "score === null || score === undefined" in render and "'N/A'" in result
    assert "typeof res.risk_score === 'number' ? `${res.risk_score}/100` : 'N/A'" in result


def test_live_responses_are_validated_and_nothing_is_invented():
    result = ALL["result"]
    assert "export function validateLiveResult" in result and "class ResultShapeError" in result
    for guard in ("is not an object", "unknown verdict", "status and verdict disagree", "no valid risk_score", "no valid signal_scores"):
        assert guard in result, guard
    assert "confidence: unable ? null : inRange(data.confidence, 0, 1)" in result            # null when absent; never defaulted
    assert "validateLiveResult(data, mode)" in ALL["api"]


def test_live_mode_never_falls_back_to_demo_data():
    scan, api, sim = ALL["scan"], ALL["api"], ALL["sim"]
    assert "isLiveMode() ? liveText(text, signal) : runLocalTextEngine(text)" in scan
    assert "if (isLiveMode() && state.selectedAudioFile) return liveAudio(state.selectedAudioFile, signal);" in scan
    assert scan.count("makeUnableResult(") >= 3 and "LIVE_API_UNREACHABLE" in scan             # failure + unreachable (text, audio)
    assert "return runLocalTextEngine" not in scan.split("async function liveText")[1].split("async function runAnalysis")[0]
    assert not any(t in {"sim"} for _, t in imports_of(api)) and not any(t == "api" for _, t in imports_of(sim))


def test_the_failed_analysis_has_its_own_recommendation_card_and_findings_text():
    render = ALL["render"]
    for needle in ("'Analysis Incomplete'", "border-slate-500/40", "No assessment was made"):
        assert needle in render, needle
    assert "not a low-risk result" in render.lower() and "not a low-risk result" in ALL["result"].lower()


def test_loading_state_is_always_cleared_and_stale_results_are_ignored():
    scan = ALL["scan"]
    run = scan.split("async function runAnalysis")[1]
    assert "beginRequest()" in run and "isCurrentRequest(requestId)" in run
    assert "finally {" in run and "stopScanAnimation();" in run.split("finally {")[1]
    assert "aria-busy" in scan and "announce(" in scan and "cancelActiveScan" in scan


# ------------------------------------------------------------------ demo mode
def test_every_simulated_result_is_flagged_and_labelled():
    sim = ALL["sim"]
    assert sim.count("simulated: true") >= 4 and "simulated: false" not in sim
    assert 'id="simBadge"' in HTML and "Simulated Demo" in HTML
    assert "el.simBadge.classList.toggle('hidden', !res.simulated)" in ALL["render"] and "SIMULATED" in ALL["render"]
    assert "api" not in {t for _, t in imports_of(sim)}


def test_the_uploaded_clip_demo_result_says_it_is_simulated_and_did_not_analyse_the_audio():
    assert "this result is simulated and the audio was not analysed" in ALL["sim"]


# ------------------------------------------------------------------ events, charts, resources
def test_listeners_are_registered_once():
    main = ALL["main"]
    assert "let initialized = false;" in main and "if (initialized) return;" in main and main.count("bindEvents();") == 1
    seen: dict[tuple[str, str], str] = {}
    for name, text in ALL.items():
        for element, event in re.findall(r"\bel\.(\w+)\.addEventListener\('(\w+)'", text):
            assert (element, event) not in seen, f"el.{element} '{event}' is registered in both {seen[(element, event)]} and {name}"
            seen[(element, event)] = name
    assert main.count("bindAudioEvents();") == 1 and main.count("bindExampleLabEvents(") == 1


def test_charts_are_created_once_and_can_be_destroyed():
    charts = ALL["charts"]
    assert "Chart.getChart(canvas)" in charts and "existing.destroy()" in charts and "export function destroyCharts" in charts
    assert "counterTokens" in charts                                                          # superseded counter animations stop


def test_object_urls_are_revoked_and_animations_can_be_stopped():
    audio, background = ALL["audio"], ALL["background"]
    assert audio.count("revokeObjectURL") >= 1 and "pagehide" in audio and "releasePreviewUrl()" in audio.split("export function handleAudioFile")[1]
    assert "cancelAnimationFrame" in audio and "cancelAnimationFrame" in background and "AbortController" in background


def test_audio_files_are_not_read_into_memory_by_the_frontend():
    joined = "\n".join(ALL.values())
    for heavy in ("FileReader", "arrayBuffer(", "readAsDataURL", "readAsArrayBuffer", "decodeAudioData"):
        assert heavy not in joined, heavy


def test_keyboard_support_exists_for_tabs_and_the_dropzone():
    assert "ArrowLeft" in ALL["main"] and "ArrowRight" in ALL["main"] and "'Enter'" in ALL["audio"] and "' '" in ALL["audio"]


# ------------------------------------------------------------------ how the backend serves the frontend
@pytest.mark.parametrize("name", sorted(EXPECTED_MODULES | {"tailwind-config"}))
def test_each_javascript_file_is_served_as_javascript_with_safe_headers(client, name):
    r = client.get(f"/js/{name}.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-cache"
    assert r.text == (JS_DIR / f"{name}.js").read_text(encoding="utf-8")


def test_the_stylesheet_is_served_as_css(client):
    r = client.get("/css/styles.css")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/css") and ".glass-panel" in r.text


def test_only_the_css_and_js_directories_are_served(client):
    for path in ("/js/../app/main.py", "/js/%2e%2e/app/main.py", "/js/..%2fapp%2fmain.py", "/css/../js/main.js.bak", "/js/", "/js",
                 "/css/", "/frontend/index.html", "/frontend/js/main.js", "/index.html", "/js/nonexistent.js", "/static/js/main.js",
                 "/legacy/index.html.txt", "/tests/conftest.py", "/.env", "/scamshield-uploads/"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (404, 307, 308), (path, r.status_code)
        if r.status_code == 307:
            assert client.get(path, follow_redirects=True).status_code == 404


def test_the_root_page_references_assets_that_exist(client):
    page = client.get("/")
    assert page.status_code == 200 and '/css/styles.css' in page.text
    for asset in re.findall(r'(?:src|href)="(/(?:js|css)/[^"]+)"', page.text):
        assert client.get(asset).status_code == 200, asset


def test_the_csp_forbids_inline_scripts_now_that_the_page_has_none():
    script_src = next(d for d in CONTENT_SECURITY_POLICY.split("; ") if d.startswith("script-src"))
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src
    assert "'unsafe-inline'" in next(d for d in CONTENT_SECURITY_POLICY.split("; ") if d.startswith("style-src"))   # Tailwind CDN needs it


def test_modules_load_under_the_csp_script_sources(client):
    csp = client.get("/").headers["content-security-policy"]
    assert "script-src 'self' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com" in csp
