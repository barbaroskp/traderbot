"""Tests for HMAC-SHA256 signature generation."""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse

from src.bingx_client import build_signature, generate_client_order_id


class TestBuildSignature:
    """Verify signature matches expected HMAC-SHA256 output."""

    def test_basic_signature(self) -> None:
        params = {"symbol": "BTC-USDT", "timestamp": "1700000000000"}
        secret = "my_secret_key"

        sig = build_signature(params, secret)

        # Manually compute expected
        sorted_params = sorted(params.items())
        qs = urllib.parse.urlencode(sorted_params, quote_via=urllib.parse.quote)
        expected = hmac.new(
            secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()

        assert sig == expected

    def test_empty_params(self) -> None:
        sig = build_signature({}, "secret")
        expected = hmac.new(
            b"secret", b"", hashlib.sha256
        ).hexdigest()
        assert sig == expected

    def test_params_sorted_alphabetically(self) -> None:
        """Ensure params are sorted regardless of insertion order."""
        params_a = {"z": "1", "a": "2", "m": "3", "timestamp": "999"}
        params_b = {"a": "2", "m": "3", "timestamp": "999", "z": "1"}

        assert build_signature(params_a, "s") == build_signature(params_b, "s")

    def test_special_characters_encoded(self) -> None:
        params = {"symbol": "BTC-USDT", "note": "hello world&more"}
        sig = build_signature(params, "sec")
        assert isinstance(sig, str)
        assert len(sig) == 64  # sha256 hex digest length

    def test_signature_deterministic(self) -> None:
        params = {"a": "1", "b": "2"}
        s1 = build_signature(params, "key")
        s2 = build_signature(params, "key")
        assert s1 == s2

    def test_different_secrets_produce_different_sigs(self) -> None:
        params = {"symbol": "ETH-USDT"}
        s1 = build_signature(params, "key1")
        s2 = build_signature(params, "key2")
        assert s1 != s2


class TestClientOrderId:
    def test_format(self) -> None:
        oid = generate_client_order_id()
        assert oid.startswith("bxa_")
        assert len(oid) == 24  # "bxa_" + 20 hex chars

    def test_uniqueness(self) -> None:
        ids = {generate_client_order_id() for _ in range(1000)}
        assert len(ids) == 1000
