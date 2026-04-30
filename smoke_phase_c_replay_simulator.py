from __future__ import annotations

import asyncio
import json
from pathlib import Path

import bot
from starlette.responses import Response


EVIDENCE_PATH = Path("phase_c_replay_evidence.json")


def mk_reply(conv_id: str, msg: str, turn: int, merchant_id: str = "m_001"):
    return bot.ReplyBody(
        conversation_id=conv_id,
        merchant_id=merchant_id,
        customer_id=None,
        from_role="merchant",
        message=msg,
        received_at="2026-04-30T00:00:00Z",
        turn_number=turn,
    )


async def push_ctx(scope: str, context_id: str, payload: dict, version: int = 1):
    resp = Response()
    body = bot.CtxBody(
        scope=scope,
        context_id=context_id,
        version=version,
        payload=payload,
        delivered_at="2026-04-30T00:00:00Z",
    )
    out = await bot.push_context(body, resp)
    assert (resp.status_code or 200) in (200, 409), (scope, context_id, out)


async def scenario_auto_reply_hell() -> dict:
    conv_id = "conv_replay_auto"
    msg = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    t2 = await bot.reply(mk_reply(conv_id, msg, 2))
    t3 = await bot.reply(mk_reply(conv_id, msg, 3))
    t4 = await bot.reply(mk_reply(conv_id, msg, 4))
    assert [t2["action"], t3["action"], t4["action"]] == ["send", "wait", "end"], (t2, t3, t4)
    return {"turn2": t2, "turn3": t3, "turn4": t4}


async def scenario_intent_transition() -> dict:
    category = {
        "slug": "dentists",
        "voice": {"tone": "peer_clinical", "vocab_taboo": ["guaranteed"]},
        "digest": [
            {
                "id": "d_intent_1",
                "title": "Fluoride 3-month recall lowers recurrence",
                "source": "JIDA Oct 2026, p.14",
                "summary": "Trial points to better outcomes for high-risk adults.",
            }
        ],
    }
    merchant = {
        "merchant_id": "m_001",
        "category_slug": "dentists",
        "identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Meera", "languages": ["en", "hi"]},
    }
    trigger = {
        "id": "trg_replay_intent",
        "scope": "merchant",
        "kind": "research_digest",
        "merchant_id": "m_001",
        "payload": {"top_item_id": "d_intent_1"},
        "suppression_key": "sup:replay:intent",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    await push_ctx("category", "dentists", category)
    await push_ctx("merchant", "m_001", merchant)
    await push_ctx("trigger", "trg_replay_intent", trigger)

    tick = await bot.tick(bot.TickBody(now="2026-04-30T00:00:00Z", available_triggers=["trg_replay_intent"]))
    assert tick["actions"], tick
    conv_id = tick["actions"][0]["conversation_id"]
    reply = await bot.reply(mk_reply(conv_id, "Ok, let's do it. What's next?", 2))
    assert reply["action"] == "send", reply
    body = (reply.get("body") or "").lower()
    assert any(k in body for k in ["draft", "confirm", "proceed", "action"]), reply
    return {"tick_action": tick["actions"][0], "turn2": reply}


async def scenario_hostile() -> dict:
    conv_id = "conv_replay_hostile"
    reply = await bot.reply(mk_reply(conv_id, "Why are you bothering me. This is useless. Stop sending these.", 2))
    assert reply["action"] == "end", reply
    return {"turn2": reply}


async def run():
    await bot.teardown()

    evidence = {
        "auto_reply_hell": await scenario_auto_reply_hell(),
        "intent_transition": await scenario_intent_transition(),
        "hostile": await scenario_hostile(),
    }
    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, ensure_ascii=False))

    print("SMOKE_PHASE_C_REPLAY_SIMULATOR: PASS")
    print("verified_facts:")
    print("- auto_reply_hell sequence is send -> wait -> end on repeated canned auto-replies")
    print("- intent_transition switches to action-forward send after explicit commitment")
    print("- hostile scenario ends conversation immediately")
    print(f"- evidence log written: {EVIDENCE_PATH}")


if __name__ == "__main__":
    asyncio.run(run())
