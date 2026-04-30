from __future__ import annotations

import asyncio
import os

import bot
from starlette.responses import Response


def mk_reply(conv_id: str, msg: str, turn: int = 2):
    return bot.ReplyBody(
        conversation_id=conv_id,
        merchant_id="m_001",
        customer_id=None,
        from_role="merchant",
        message=msg,
        received_at="2026-04-30T00:00:00Z",
        turn_number=turn,
    )


async def run():
    await bot.teardown()

    # --- detector fixture checks ---
    detector_fixtures = [
        ("STOP messaging me", "hard_stop"),
        ("मैसेज मत करो", "hard_stop"),
        ("மெசேஜ் அனுப்பாதே", "hard_stop"),
        ("మెసేజ్ పంపొద్దు", "hard_stop"),
        ("ಸಂಪರ್ಕಿಸಬೇಡಿ", "hard_stop"),
        ("এটা স্প্যাম, যোগাযোগ করবেন না", "hard_stop"),
        ("this is useless spam", "high_hostile"),
        ("bahut pareshan kar rahe ho", "medium_frustration"),
    ]
    for text, expected_kind in detector_fixtures:
        sig = bot.detect_hostility_signal(text)
        assert sig.kind == expected_kind, f"detector mismatch for '{text}': {sig.kind} != {expected_kind}"

    # --- reply policy fixtures ---
    # hard stop -> end
    r1 = await bot.reply(mk_reply("conv_hard_stop", "not interested. stop messaging"))
    assert r1["action"] == "end", r1

    # high hostile -> end
    r2 = await bot.reply(mk_reply("conv_high_hostile", "this is useless spam"))
    assert r2["action"] == "end", r2

    # medium frustration -> send once, then end on repeat
    conv_id = "conv_medium_escalation"
    m1 = await bot.reply(mk_reply(conv_id, "why are you bothering me", 2))
    assert m1["action"] == "send", m1
    m2 = await bot.reply(mk_reply(conv_id, "still annoying, dont disturb", 3))
    assert m2["action"] == "end", m2

    # ended conversation should not reopen
    m3 = await bot.reply(mk_reply(conv_id, "ok continue", 4))
    assert m3["action"] == "end", m3

    # auto-reply progression remains intact
    auto_id = "conv_auto_phase_c"
    a1 = await bot.reply(mk_reply(auto_id, "Thank you for contacting us. Our team will respond shortly.", 2))
    assert a1["action"] == "send", a1
    a2 = await bot.reply(mk_reply(auto_id, "Thank you for contacting us. Our team will respond shortly.", 3))
    assert a2["action"] == "wait", a2
    a3 = await bot.reply(mk_reply(auto_id, "Thank you for contacting us. Our team will respond shortly.", 4))
    assert a3["action"] == "end", a3

    # auto-reply mode override: wait on first occurrence
    os.environ["AUTO_REPLY_FIRST_ACTION"] = "wait"
    wait_mode_id = "conv_auto_wait_mode"
    w1 = await bot.reply(mk_reply(wait_mode_id, "Thank you for contacting us. Our team will respond shortly.", 2))
    assert w1["action"] == "wait", w1
    os.environ.pop("AUTO_REPLY_FIRST_ACTION", None)

    # artifact fulfillment on explicit ask in same turn
    async def push_ctx(scope: str, context_id: str, payload: dict):
        resp = Response()
        body = bot.CtxBody(
            scope=scope,
            context_id=context_id,
            version=1,
            payload=payload,
            delivered_at="2026-04-30T00:00:00Z",
        )
        out = await bot.push_context(body, resp)
        assert (resp.status_code or 200) in (200, 409), out

    category = {
        "slug": "dentists",
        "voice": {"tone": "peer_clinical", "vocab_taboo": ["guaranteed"]},
        "digest": [
            {
                "id": "d_1",
                "title": "Fluoride 3-month recall lowers recurrence",
                "source": "JIDA Oct 2026, p.14",
                "summary": "Trial points to better outcomes for high-risk adults.",
            }
        ],
    }
    merchant = {
        "merchant_id": "m_001",
        "category_slug": "dentists",
        "identity": {"name": "Dr. Clinic", "owner_first_name": "Meera", "languages": ["en", "hi"]},
    }
    trigger = {
        "id": "trg_artifact",
        "scope": "merchant",
        "kind": "research_digest",
        "merchant_id": "m_001",
        "payload": {"top_item_id": "d_1"},
        "suppression_key": "sup:artifact",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    await push_ctx("category", "dentists", category)
    await push_ctx("merchant", "m_001", merchant)
    await push_ctx("trigger", "trg_artifact", trigger)

    tick = await bot.tick(bot.TickBody(now="2026-04-30T00:00:00Z", available_triggers=["trg_artifact"]))
    assert tick["actions"], tick
    conv_id_artifact = tick["actions"][0]["conversation_id"]
    ar = await bot.reply(mk_reply(conv_id_artifact, "Yes, please send the abstract and draft WhatsApp copy.", 2))
    assert ar["action"] == "send", ar
    assert "Abstract snapshot:" in ar["body"], ar
    assert "Draft message:" in ar["body"], ar

    # nudge state closure flag should be set for ended conversation
    conv = bot.conversations[conv_id]
    nk = bot._conv_nudge_key(conv)
    ns = bot.nudge_state.get(nk)
    assert ns is not None and ns.closed is True, "nudge state not marked closed for ended conversation"

    print("SMOKE_PHASE_C_DIRECT: PASS")
    print("verified_facts:")
    print("- multilingual hard-stop detection (EN/HI/TA/TE/KN/BN sample fixtures) passes")
    print("- high-hostile messages terminate immediately")
    print("- medium frustration de-escalates and terminates on repeated signals")
    print("- ended conversation cannot be reopened via later reply")
    print("- auto-reply progression remains send -> wait -> end")
    print("- first auto-reply can be policy-switched to immediate wait mode")
    print("- explicit artifact ask returns same-turn abstract + draft style reply")
    print("- ended conversation marks nudge-state closure")


if __name__ == "__main__":
    asyncio.run(run())
