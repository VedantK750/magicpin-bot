from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from pydantic import ValidationError
from starlette.responses import Response

import bot


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_category(slug: str) -> dict:
    return {
        "slug": slug,
        "voice": {"tone": "peer", "vocab_taboo": ["guaranteed"]},
        "digest": [{"id": f"d_{slug}_1", "title": f"{slug} update", "source": f"{slug} source"}],
        "peer_stats": {"avg_ctr": 0.03},
        "offer_catalog": [],
    }


def build_merchant(mid: str, slug: str) -> dict:
    return {
        "merchant_id": mid,
        "category_slug": slug,
        "identity": {"name": f"Merchant {mid}", "owner_first_name": "Owner", "languages": ["en", "hi"]},
        "performance": {"views": 1000, "calls": 12, "ctr": 0.02},
        "offers": [{"id": "o1", "title": "Haircut @ ₹99", "status": "active"}],
    }


def build_customer(cid: str, mid: str) -> dict:
    return {
        "customer_id": cid,
        "merchant_id": mid,
        "identity": {"name": "Priya", "language_pref": "hi-en mix"},
        "preferences": {"channel": "whatsapp"},
    }


def build_trigger(tid: str, mid: str, cid: str | None = None) -> dict:
    return {
        "id": tid,
        "scope": "customer" if cid else "merchant",
        "kind": "recall_due" if cid else "research_digest",
        "source": "internal",
        "merchant_id": mid,
        "customer_id": cid,
        "payload": {"top_item_id": "d_salons_1"} if not cid else {"service_due": "6_month_cleaning"},
        "urgency": 2,
        "suppression_key": f"sup:{tid}",
        "expires_at": "2099-01-01T00:00:00Z",
    }


def assert_action_shape(action: dict):
    required = [
        "conversation_id",
        "merchant_id",
        "customer_id",
        "send_as",
        "trigger_id",
        "template_name",
        "template_params",
        "body",
        "cta",
        "suppression_key",
        "rationale",
    ]
    missing = [k for k in required if k not in action]
    assert not missing, f"missing fields: {missing}"


async def push_context(scope: str, context_id: str, version: int, payload: dict):
    response = Response()
    body = bot.CtxBody(scope=scope, context_id=context_id, version=version, payload=payload, delivered_at=iso_now())
    data = await bot.push_context(body, response)
    return response.status_code or 200, data


async def run():
    # reset
    td = await bot.teardown()
    assert td["ok"] is True

    # healthz + metadata
    hz = await bot.healthz()
    assert hz["status"] == "ok"
    md = await bot.metadata()
    for k in ["team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"]:
        assert k in md, f"metadata missing {k}"

    # malformed model validation branch (equivalent to HTTP 422)
    malformed_raised = False
    try:
        bot.CtxBody(scope="category", context_id="x", version=1, payload={})  # missing delivered_at
    except ValidationError:
        malformed_raised = True
    assert malformed_raised, "expected CtxBody validation failure for missing delivered_at"

    # invalid scope -> 400
    sc, data = await push_context("bad_scope", "x", 1, {"x": 1})
    assert sc == 400, (sc, data)
    assert data["reason"] == "invalid_scope"

    # valid + stale conflict
    sc, data = await push_context("category", "salons", 1, build_category("salons"))
    assert sc == 200 and data["accepted"] is True, (sc, data)
    sc, data = await push_context("category", "salons", 1, build_category("salons"))
    assert sc == 409 and data["reason"] == "stale_version", (sc, data)

    # warmup count smoke: 5 + 50 + 200
    cats = ["dentists", "salons", "restaurants", "gyms", "pharmacies"]
    for slug in cats:
        sc, _ = await push_context("category", slug, 1, build_category(slug))
        assert sc in (200, 409)
    for i in range(1, 51):
        mid = f"m_{i:03d}"
        slug = cats[i % len(cats)]
        sc, _ = await push_context("merchant", mid, 1, build_merchant(mid, slug))
        assert sc == 200
    for i in range(1, 201):
        cid = f"c_{i:03d}"
        mid = f"m_{((i - 1) % 50) + 1:03d}"
        sc, _ = await push_context("customer", cid, 1, build_customer(cid, mid))
        assert sc == 200
    hz2 = await bot.healthz()
    counts = hz2["contexts_loaded"]
    assert counts["category"] == 5, counts
    assert counts["merchant"] == 50, counts
    assert counts["customer"] == 200, counts

    # tick basic action contract
    sc, _ = await push_context("trigger", "trg_001", 1, build_trigger("trg_001", "m_001"))
    assert sc == 200
    tick_res = await bot.tick(bot.TickBody(now=iso_now(), available_triggers=["trg_001"]))
    assert "actions" in tick_res and isinstance(tick_res["actions"], list)
    assert len(tick_res["actions"]) <= 20
    if tick_res["actions"]:
        action = tick_res["actions"][0]
        assert_action_shape(action)
        assert "http://" not in action["body"].lower()
        assert "https://" not in action["body"].lower()

    # pair uniqueness guard
    tids = []
    for i in range(2, 12):
        tid = f"trg_{i:03d}"
        tids.append(tid)
        sc, _ = await push_context("trigger", tid, 1, build_trigger(tid, "m_001"))
        assert sc == 200
    tick2 = await bot.tick(bot.TickBody(now=iso_now(), available_triggers=tids))
    pairs = {(a["merchant_id"], a["conversation_id"]) for a in tick2["actions"]}
    assert len(pairs) == len(tick2["actions"]), "duplicate (merchant_id, conversation_id) pair found"

    # elapsed-time guard smoke (slow compose)
    original_compose = bot.compose
    try:
        def slow_compose(*args, **kwargs):
            time.sleep(0.7)
            return original_compose(*args, **kwargs)

        bot.compose = slow_compose  # type: ignore[assignment]
        many = []
        for i in range(100, 140):
            tid = f"trg_{i:03d}"
            many.append(tid)
            sc, _ = await push_context("trigger", tid, 1, build_trigger(tid, "m_001"))
            assert sc == 200
        t0 = time.monotonic()
        _ = await bot.tick(bot.TickBody(now=iso_now(), available_triggers=many))
        elapsed = time.monotonic() - t0
        assert elapsed < 11.5, f"tick elapsed too high: {elapsed:.2f}s"
    finally:
        bot.compose = original_compose  # type: ignore[assignment]

    print("SMOKE_PHASE_AB_DIRECT: PASS")
    print("verified_facts:")
    print("- /v1/healthz and /v1/metadata return required keys")
    print("- /v1/context invalid scope path returns status 400 + reason invalid_scope")
    print("- /v1/context stale version path returns status 409 + reason stale_version")
    print("- CtxBody malformed payload fails model validation (equivalent HTTP 422 path)")
    print("- Warmup-context counts verified in-memory: category=5, merchant=50, customer=200")
    print("- /v1/tick action schema and required fields validated")
    print("- per-tick (merchant_id, conversation_id) uniqueness assertion passed")
    print("- tick elapsed-time guard smoke passed under slow compose patch")


if __name__ == "__main__":
    asyncio.run(run())
