"""Scam Example Lab (replaced GET PROMPT): static checks on the page and the sample data. The behaviour (modal,
keyboard, load without submit) was verified in a browser; see docs/frontend.md."""
from __future__ import annotations

import re

from app.core.config import Settings
from tests.api.test_frontend_contract import ALL, CODE, HTML

EXAMPLES_JS = ALL["examples"]
MAIN = CODE["main"]
EXPECTED_TITLES = ["Banking impersonation", "Digital arrest scam", "Fake job offer", "Phishing message",
                   "Fake delivery notification", "Investment scam", "OTP and credential theft",
                   "Legitimate message (safe comparison)"]


def parse_examples() -> list[dict]:
    block = EXAMPLES_JS[EXAMPLES_JS.index("export const EXAMPLES"):EXAMPLES_JS.index("let opener")]
    entries = re.findall(r"\{\s*id: '([^']+)',\s*title: '([^']+)',\s*pattern: '((?:[^'\\]|\\.)*)',\s*text: '((?:[^'\\]|\\.)*)',(\s*legitimate: true,)?", block)
    return [dict(id=i, title=t, pattern=p, text=x, legitimate=bool(l)) for i, t, p, x, l in entries]


def test_the_eight_required_scenarios_exist_in_order():
    assert [e["title"] for e in parse_examples()] == EXPECTED_TITLES


def test_each_scenario_has_a_realistic_message_and_a_description():
    for e in parse_examples():
        assert len(e["text"]) >= 120 and len(e["pattern"]) >= 40, e["id"]
        assert len(e["text"]) <= Settings().max_text_length


def test_exactly_one_legitimate_example_and_it_is_the_safe_comparison():
    legit = [e for e in parse_examples() if e["legitimate"]]
    assert [e["id"] for e in legit] == ["legitimate_message"]
    assert "never share" in legit[0]["text"].lower()


def test_samples_contain_no_scores_or_verdicts():
    for e in parse_examples():
        blob = (e["text"] + " " + e["pattern"]).lower()
        assert not re.search(r"risk score|\b\d{1,3}\s*/\s*100\b|high risk|low risk|\bverdict\b|suspicious \(", blob), e["id"]
    assert not re.search(r"risk_score|verdict|signal_scores", EXAMPLES_JS.split("let opener")[0].replace("NO scores or verdicts", ""))


def test_links_in_samples_use_non_resolving_example_domains():
    for e in parse_examples():
        for url in re.findall(r"https?://[^\s,;)]+", e["text"]):
            host = re.sub(r"^https?://", "", url).split("/")[0].split("?")[0]
            assert host.rstrip(".").endswith(".example"), (e["id"], url)


def test_examples_are_labelled_fictional_in_the_page():
    assert HTML.count("Fictional training sample") >= 1 and "fictional training sample" in HTML
    assert "LOAD EXAMPLE" in HTML and "SCAM EXAMPLE LAB" in HTML


def test_the_old_get_prompt_feature_is_gone():
    for gone in ("GET PROMPT", "getPromptInput", "btnRunGetPrompt", "Audit Prompt", "prompt-task-btn"):
        assert gone not in HTML, gone
    for name, text in CODE.items():
        assert "bindPromptEvents" not in text and "buildPromptForTask" not in text, name


def test_loading_fills_the_input_but_never_submits():
    handler = MAIN[MAIN.index("bindExampleLabEvents({"):MAIN.index("window.addEventListener('pagehide'")]
    assert "el.messageInput.value = example.text" in handler and "refreshTextPreview()" in handler
    for forbidden in ("triggerTextScan", "btnAnalyzeText", ".click()", "fetch(", "analyzeText"):
        assert forbidden not in handler
    lab = CODE["examples"]
    for forbidden in ("triggerTextScan", "fetch(", "XMLHttpRequest", "localStorage", "sessionStorage"):
        assert forbidden not in lab


def test_the_lab_is_a_modal_with_keyboard_support():
    code = CODE["examples"]
    for needle in ("'Escape'", "'Tab'", "ArrowDown", "opener.focus", "aria-checked", "overflow-hidden"):
        assert needle in code, needle


def test_the_lab_is_wired_exactly_once():
    assert MAIN.count("bindExampleLabEvents(") == 1 and CODE["examples"].count("el.btnOpenExampleLab.addEventListener") == 1
