"""Tests for idempotency guarantees."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.bingx_client import generate_client_order_id
from src.storage import Storage


class TestIdempotency:
    def test_client_order_id_unique(self) -> None:
        """1000 generated IDs must all be unique."""
        ids = [generate_client_order_id() for _ in range(1000)]
        assert len(set(ids)) == 1000

    def test_duplicate_order_id_rejected_by_db(self, db) -> None:
        """DB enforces UNIQUE on client_order_id."""
        now = datetime.now(timezone.utc).isoformat()
        oid = generate_client_order_id()

        db.insert("orders", {
            "client_order_id": oid,
            "ts": now,
            "symbol": "BTC-USDT",
            "side": "LONG",
            "order_type": "LIMIT",
            "qty": 0.001,
            "status": "PENDING",
            "is_paper": 1,
            "reduce_only": 0,
            "updated_at": now,
        })

        # Second insert with same client_order_id should fail
        with pytest.raises(Exception):
            db.insert("orders", {
                "client_order_id": oid,
                "ts": now,
                "symbol": "BTC-USDT",
                "side": "LONG",
                "order_type": "LIMIT",
                "qty": 0.001,
                "status": "PENDING",
                "is_paper": 1,
                "reduce_only": 0,
                "updated_at": now,
            })

    def test_order_id_format_consistent(self) -> None:
        for _ in range(100):
            oid = generate_client_order_id()
            assert oid.startswith("bxa_")
            assert len(oid) == 24
            # Only hex chars after prefix
            assert all(c in "0123456789abcdef" for c in oid[4:])
