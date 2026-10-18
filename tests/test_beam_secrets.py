"""Domain-scoped secrets: substitution, denial, redaction. Offline."""

from jev_ultrafast import secrets as beam_secrets
from jev_ultrafast.trace import scrub


def test_substitute_on_allowed_host_and_subdomain(monkeypatch):
    monkeypatch.setenv("JEV_SECRET_API", "s3cr3t-value")
    monkeypatch.setenv("JEV_SECRET_API_DOMAINS", "example.test")
    text, error = beam_secrets.substitute("key=<secret>API</secret>", "https://example.test/x")
    assert (text, error) == ("key=s3cr3t-value", None)
    text, error = beam_secrets.substitute("<secret>API</secret>", "https://sub.example.test/")
    assert (text, error) == ("s3cr3t-value", None)


def test_substitute_denies_other_hosts_and_missing(monkeypatch):
    monkeypatch.setenv("JEV_SECRET_API", "s3cr3t-value")
    monkeypatch.setenv("JEV_SECRET_API_DOMAINS", "example.test")
    text, error = beam_secrets.substitute("<secret>API</secret>", "https://evil.test/")
    assert text is None
    assert "not allowed on this host" in error
    assert "s3cr3t-value" not in error
    text, error = beam_secrets.substitute("<secret>NOPE</secret>", "https://example.test/")
    assert text is None
    assert "not configured" in error
    assert beam_secrets.unavailable("<secret>API</secret> x <secret>NOPE</secret>", "https://evil.test/") == [
        "API", "NOPE",
    ]


def test_plain_text_passes_through():
    assert beam_secrets.substitute("hello", "https://example.test/") == ("hello", None)
    assert beam_secrets.unavailable("hello", "https://example.test/") == []


def test_trace_scrub_redacts_secret_values(monkeypatch):
    monkeypatch.setenv("JEV_SECRET_API", "s3cr3t-value")
    monkeypatch.setenv("JEV_SECRET_API_DOMAINS", "example.test")
    assert scrub("typed s3cr3t-value here") == "typed [redacted] here"
    assert scrub("placeholder <secret>API</secret> stays") == "placeholder <secret>API</secret> stays"
