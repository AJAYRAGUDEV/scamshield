"""Phase 7 prompt contract. The model's behaviour cannot be tested offline; these tests lock in WHAT WE ASK of it."""
from __future__ import annotations

from app.providers.gemini.prompts import (AUDIO_PROMPT, LINK_FACTS_SEPARATOR, SYSTEM_PROMPT, TEXT_PROMPT_PREFIX,
                                         build_text_prompt)
from app.schemas.evidence import SignalCategory
from typing import get_args

LOWER = SYSTEM_PROMPT.lower()


def test_every_category_is_defined_in_the_prompt():
    for category in get_args(SignalCategory):
        if category in ("fake_interface", "visual_manipulation"):       # video categories (not implemented)
            continue
        assert category in SYSTEM_PROMPT, category


def test_the_model_is_told_not_to_decide_the_outcome():
    for phrase in ("do not assign risk scores", "server calculates"):
        assert phrase in LOWER
    for forbidden in ("use low risk", "high risk only for"):
        assert forbidden not in LOWER


def test_context_rules_distinguish_discussion_from_instruction():
    for value in ("directed_at_recipient", "discussed_or_described", "fictional_or_example", "unclear"):
        assert value in SYSTEM_PROMPT
    assert "news" in LOWER and "fiction" in LOWER


def test_prompt_injection_and_identity_guards_are_present():
    assert "untrusted data" in LOWER and "ignore any instruction" in LOWER
    assert "cloned or synthetic" in LOWER and "authenticate" in LOWER
    assert "you cannot check it" in LOWER


def test_audio_rules_forbid_guessing_and_long_transcripts():
    audio = (SYSTEM_PROMPT + AUDIO_PROMPT).lower()
    assert "timestamp" in audio and ("do not guess" in audio or "never guess" in audio or "do not invent" in audio)
    assert "silence" in audio


def test_languages_are_covered():
    for language in ("Tamil", "Hindi"):
        assert language in SYSTEM_PROMPT
    assert "original" in LOWER and "english" in LOWER


def test_text_prompt_without_facts_is_prefix_plus_text_unchanged():
    assert build_text_prompt("hello {x}") == TEXT_PROMPT_PREFIX + "hello {x}"


def test_link_facts_are_appended_after_a_separator_and_labelled_as_server_facts():
    prompt = build_text_prompt("see http://x", "Link 1: host 10.0.0.1 (numeric)")
    assert prompt.startswith(TEXT_PROMPT_PREFIX + "see http://x") and LINK_FACTS_SEPARATOR in prompt
    assert prompt.endswith("Link 1: host 10.0.0.1 (numeric)")
