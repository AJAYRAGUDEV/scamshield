"""The originals are still backed up, and the compatibility shim really is a shim."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_legacy_backups_are_intact():
    assert (ROOT / "legacy" / "server.py").stat().st_size == 12727
    assert (ROOT / "legacy" / "test.py").stat().st_size == 217
    assert (ROOT / "legacy" / "index.html.txt").stat().st_size == 99072


def test_root_test_py_is_untouched():
    assert (ROOT / "test.py").read_bytes() == (ROOT / "legacy" / "test.py").read_bytes()


def test_server_py_is_a_thin_compatibility_shim():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "from app.main import app" in source
    assert len(source.splitlines()) < 15 and "genai" not in source and "FastAPI(" not in source


def test_original_element_ids_survive_except_the_removed_api_key_form():
    """The frontend was adjusted in Phases 4-5. Every original element ID must still exist (the JS looks them
    all up at start-up) EXCEPT the three belonging to the API-key form, which Phase 5 removed on purpose:
    the Gemini key now lives only on the server."""
    import re

    original = (ROOT / "legacy" / "index.html.txt").read_text(encoding="utf-8")
    active = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    original_ids = set(re.findall(r'\bid="([^"]+)"', original))
    active_ids = set(re.findall(r'\bid="([^"]+)"', active))
    # The GET PROMPT task bar was replaced by the Scam Example Lab, so its ids are gone too.
    prompt_bar = {"getPromptTaskbar", "getPromptInput", "btnClearPromptInput", "btnRunGetPrompt", "getPromptOutputPanel",
                  "getPromptOutputTitle", "getPromptOutputText", "btnCopyGeneratedPrompt", "btnSendPromptToScanner",
                  "btnClosePromptPanel"}
    assert original_ids - active_ids == {"apiKeyInput", "btnToggleKeyVis", "btnClearKey"} | prompt_bar
    assert active_ids - original_ids == {"simBadge", "actionCard", "actionTitle",
                                         "backendStatusDot", "backendStatusText", "btnRecheckBackend", "srAnnouncer",
                                         "languageBadge", "limitationsBlock", "limitationsList", "urlFindings", "urlFindingsList",
                                         "exampleLabBar", "btnOpenExampleLab", "exampleLab", "exampleLabBackdrop", "exampleLabPanel",
                                         "exampleLabTitle", "exampleLabIntro", "btnCloseExampleLab", "btnCancelExampleLab", "exampleList",
                                         "exampleEmpty", "exampleDetails", "exampleTitle", "exampleDescription", "exampleLegitNote",
                                         "exampleSampleText", "btnLoadExample",
                                         "coverageBadge", "tabVideo", "panelVideo", "videoDropzone", "videoFileInput", "selectedVideoBadge",
                                         "selectedVideoName", "selectedVideoSize", "selectedVideoLength", "btnReplaceVideo",
                                         "btnRemoveVideo", "videoError", "videoModeNote", "btnAnalyzeVideo", "btnStopVideoWait"}


def test_single_active_frontend_file():
    assert (ROOT / "frontend" / "index.html").is_file()
    assert not (ROOT / "index.html").exists() and not (ROOT / "index.html.txt").exists()
