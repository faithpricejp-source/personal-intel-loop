from __future__ import annotations

from personal_intel_loop.schemas import canonicalize_url


def test_canonicalize_url_strips_tracking_params():
    raw = "HTTPS://Example.COM/article/?utm_source=x&utm_medium=y&ref=foo&b=2&a=1#frag"
    assert canonicalize_url(raw) == "https://example.com/article?a=1&b=2"


def test_canonicalize_url_keeps_equivalent_variants_stable():
    left = "https://example.com/article/?a=1&b=2"
    right = "https://example.com/article?a=1&b=2#ignored"
    assert canonicalize_url(left) == canonicalize_url(right)
