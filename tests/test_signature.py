"""Tests for HMAC-SHA256 signature generation."""

from __future__ import annotations

import hashlib
import hmac

from src.bingx_client import build_query_string, build_signature, generate_client_order_id


class TestBuildSignature:
    """Verify signature matches expected HMAC-SHA256 output."""

    def test_basic_signature(self) -> None:
        params = {"symbol": "BTC-USDT", "timestamp": "1700000000000"}
        secret = "my_secret_key"

        qs = build_query_string(params)
        sig = build_signature(qs, secret)

        expected = hmac.new(
            secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()

        assert sig == expected

    def test_empty_params(self) -> None:
        qs = build_query_string({})
        sig = build_signature(qs, "secret")
        expected = hmac.new(
            b"secret", b"", hashlib.sha256
        ).hexdigest()
        assert sig == expected

    def test_params_sorted_alphabetically(self) -> None:
        """Ensure params are sorted regardless of insertion order."""
        params_a = {"z": "1", "a": "2", "m": "3", "timestamp": "999"}
        params_b = {"a": "2", "m": "3", "timestamp": "999", "z": "1"}

        qs_a = build_query_string(params_a)
        qs_b = build_query_string(params_b)
        assert build_signature(qs_a, "s") == build_signature(qs_b, "s")

    def test_special_characters_encoded(self) -> None:
        params = {"symbol": "BTC-USDT", "note": "hello world&more"}
        qs = build_query_string(params)
        sig = build_signature(qs, "sec")
        assert isinstance(sig, str)
        assert len(sig) == 64  # sha256 hex digest length

    def test_signature_deterministic(self) -> None:
        params = {"a": "1", "b": "2"}
        qs = build_query_string(params)
        s1 = build_signature(qs, "key")
        s2 = build_signature(qs, "key")
        assert s1 == s2

    def test_different_secrets_produce_different_sigs(self) -> None:
        params = {"symbol": "ETH-USDT"}
        qs = build_query_string(params)
        s1 = build_signature(qs, "key1")
        s2 = build_signature(qs, "key2")
        assert s1 != s2


class TestBuildQueryString:
    def test_sorted_output(self) -> None:
        qs = build_query_string({"z": "1", "a": "2"})
        assert qs == "a=2&z=1"

    def test_empty(self) -> None:
        assert build_query_string({}) == ""


class TestClientOrderId:
    def test_format(self) -> None:
        oid = generate_client_order_id()
        assert oid.startswith("bxa_")
        assert len(oid) == 24  # "bxa_" + 20 hex chars

    def test_uniqueness(self) -> None:
        ids = {generate_client_order_id() for _ in range(1000)}
        assert len(ids) == 1000
