"""URL structure analysis: pure functions, no network, informational vs scored indicators."""
from __future__ import annotations

import socket

import pytest

from app.services.url_analysis import STRUCTURE_ONLY, analyze_urls, extract_urls, registrable_domain


def indicators(text):
    return [set(f.indicators) for f in analyze_urls(text).findings]


def test_no_links_means_no_findings_and_no_prompt_facts():
    result = analyze_urls("Hello, please call me tomorrow.")
    assert result.findings == () and result.signals == () and result.prompt_facts is None


def test_every_finding_states_the_reputation_was_not_verified():
    result = analyze_urls("see https://example.org/page and http://1.2.3.4/x")
    assert result.findings and all(f.reputation_verified is False and STRUCTURE_ONLY in f.note for f in result.findings)


def test_analysis_never_touches_the_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    analyze_urls("http://paypa1-login.example/verify https://bit.ly/x http://10.0.0.1/a")


def test_paths_queries_and_credentials_are_not_shown_in_the_display():
    finding = analyze_urls("http://user:secret@evil.example/path?token=abc123").findings[0]
    assert "secret" not in finding.display and "token" not in finding.display and "abc123" not in finding.display
    assert finding.domain == "evil.example" and "userinfo_in_url" in finding.indicators


@pytest.mark.parametrize("url, label", [
    ("http://192.168.1.5/login", "ip_address_host"),
    ("http://paypa1-secure.example/", "possible_brand_impersonation"),
    ("http://hdfcbank.account-verify.example/", "possible_brand_impersonation"),
    ("http://xn--pypal-4ve.example/", "punycode_domain"),
    ("http://a.b.c.d.example.com/", "excessive_subdomains"),
    ("https://example.com/go?u=https://evil.example", "redirect_in_query"),
    ("http://a-b-c-d-e.example/", "many_hyphens"),
])
def test_structural_indicators(url, label):
    assert label in indicators(f"open {url} now")[0]


def test_link_text_that_names_another_domain_is_flagged():
    assert "mismatched_link_text" in indicators("[www.hdfcbank.com](http://evil.example/login)")[0]
    assert "mismatched_link_text" in indicators('<a href="http://evil.example/x">hdfcbank.com</a>')[0]


def test_shorteners_and_unfamiliar_domains_are_shown_but_not_scored():
    result = analyze_urls("https://bit.ly/abc and https://my-small-bakery.example/menu")
    assert "shortened_url" in result.findings[0].indicators
    assert result.signals == ()                                   # informational only: nothing enters the risk engine


def test_official_brand_domains_are_not_flagged():
    for url in ("https://www.paypal.com/signin", "https://netbanking.hdfcbank.com/", "https://www.amazon.in/orders", "https://incometax.gov.in/"):
        found = indicators(url)[0]
        assert "possible_brand_impersonation" not in found, url


def test_scored_indicators_become_suspicious_link_signals_with_cautious_wording():
    result = analyze_urls("http://paypa1-secure-login.example/verify")
    assert result.signals and all(s["category"] == "suspicious_link" for s in result.signals)
    assert all("verified" in s["evidence"] for s in result.signals)
    assert all(word not in s["evidence"].lower() for s in result.signals for word in ("malicious", "phishing site", "is a scam"))


def test_limits_on_how_many_links_are_processed():
    text = " ".join(f"http://192.168.0.{i}/x" for i in range(60))
    result = analyze_urls(text)
    assert len(result.findings) <= 20 and len(result.signals) <= 5


def test_huge_or_malformed_input_does_not_raise():
    for text in ("http://", "https://[::1", "http://" + "a" * 5000 + ".com", "www.", "http://%zz.example", "\x00http://x\x00"):
        analyze_urls(text)


def test_prompt_facts_describe_structure_and_say_the_link_was_not_visited():
    facts = analyze_urls("http://192.168.0.1/login").prompt_facts
    assert facts and "192.168.0.1" in facts and "structure" in facts.lower()


def test_trailing_punctuation_is_not_part_of_the_link():
    assert [e.raw for e in extract_urls("Visit https://example.org/a.")] == ["https://example.org/a"]


@pytest.mark.parametrize("host, expected", [
    ("www.example.com", "example.com"), ("a.b.example.co.in", "example.co.in"), ("login.hdfcbank.com", "hdfcbank.com"),
])
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected
