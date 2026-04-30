import hashlib
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Union

from fastapi import FastAPI, Response
from pydantic import BaseModel, Field


app = FastAPI(title="magicpin-challenge-bot", version="0.1.0")
START_TS = time.time()

ALLOWED_SCOPES = {"category", "merchant", "customer", "trigger"}
URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)

# Global in-memory state
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, "ConversationState"] = {}
suppression_sent: dict[tuple[str, str, str], str] = {}
nudge_state: dict[tuple[str, str, str], "NudgeState"] = {}
conv_counter = 0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_dt(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def norm_text(text: str) -> str:
    # Unicode normalize + punctuation strip + whitespace collapse.
    t = unicodedata.normalize("NFKC", text or "").lower().strip()
    t = re.sub(r"[^\w\s\u0900-\u097F\u0B80-\u0BFF\u0C00-\u0C7F\u0C80-\u0CFF\u0980-\u09FF\u0D00-\u0D7F\u0A80-\u0AFF\u0A00-\u0A7F]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def text_hash(text: str) -> str:
    return hashlib.sha256(norm_text(text).encode("utf-8")).hexdigest()


def has_url(text: str) -> bool:
    return bool(URL_RE.search(text or ""))


def clip(text: str, limit: int = 700) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[: limit - 1].rstrip() + "…"


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default


def pct(v: Any) -> Optional[str]:
    if not isinstance(v, (int, float)):
        return None
    if -1.0 <= float(v) <= 1.0:
        return f"{float(v) * 100:.1f}%"
    return f"{float(v):.1f}%"


def human_kind(kind: str) -> str:
    return (kind or "update").replace("_", " ")


def merchant_name(merchant: dict[str, Any]) -> str:
    return merchant.get("identity", {}).get("name") or merchant.get("merchant_id") or "there"


def owner_name(merchant: dict[str, Any]) -> str:
    return merchant.get("identity", {}).get("owner_first_name") or merchant_name(merchant)


def language_pref(customer: Optional[dict[str, Any]], merchant: dict[str, Any]) -> str:
    if customer:
        lp = customer.get("identity", {}).get("language_pref")
        if isinstance(lp, str) and lp:
            return lp.lower()
    langs = merchant.get("identity", {}).get("languages", [])
    if "hi" in langs:
        return "hi-en mix"
    return "english"


HARD_STOP_PATTERNS = [
    # English core
    r"\bnot interested\b",
    r"\bstop\b",
    r"\bstop messaging\b",
    r"\bdon t message\b",
    r"\bdont message\b",
    r"\bunsubscribe\b",
    r"\bremove me\b",
    r"\bopt out\b",
    r"\bdo not contact\b",
    # Hindi roman + Devanagari
    r"\bmsg mat karo\b",
    r"\bmessage mat karo\b",
    r"\bband karo\b",
    r"\bband kijiye\b",
    r"\bbaat band\b",
    r"मैसेज मत करो",
    r"संदेश मत भेजो",
    r"बंद करो",
    r"रोक दो",
    # Tamil
    r"மெசேஜ் அனுப்பாதே",
    r"மேசேஜ் அனுப்பாதே",
    r"தொடர்பு கொள்ளாதே",
    # Telugu
    r"మెసేజ్ పంపొద్దు",
    r"సంప్రదించొద్దు",
    # Kannada
    r"ಮೆಸೇಜ್ ಮಾಡಬೇಡಿ",
    r"ಸಂಪರ್ಕಿಸಬೇಡಿ",
    # Marathi
    r"मेसेज करू नका",
    r"संपर्क करू नका",
    # Bengali
    r"মেসেজ করবেন না",
    r"যোগাযোগ করবেন না",
    # Malayalam
    r"മെസ്സേജ് അയക്കരുത്",
    r"ബന്ധപ്പെടരുത്",
    # Gujarati
    r"મેસેજ ન મોકલો",
    r"સંપર્ક ન કરો",
    # Punjabi
    r"ਮੇਸੇਜ ਨਾ ਕਰੋ",
    r"ਸੰਪਰਕ ਨਾ ਕਰੋ",
]

HIGH_HOSTILE_PATTERNS = [
    r"\buseless\b",
    r"\bspam\b",
    r"\bfraud\b",
    r"\bscam\b",
    r"\bbakw(a|aa)s\b",
    r"\bbekaar\b",
    r"\bfaltu\b",
    r"बेकार",
    r"फालतू",
    r"बकवास",
]

MEDIUM_FRUSTRATION_PATTERNS = [
    r"\bwhy are you bothering\b",
    r"\btoo many messages\b",
    r"\bdon t disturb\b",
    r"\bdont disturb\b",
    r"\bnot useful\b",
    r"\bannoying\b",
    r"\bpareshan\b",
    r"\bpareshan mat karo\b",
    r"परेशान",
    r"मत भेजो",
    r"बार बार",
]


@dataclass
class HostilitySignal:
    kind: Literal["none", "hard_stop", "high_hostile", "medium_frustration"] = "none"
    confidence: Literal["none", "low", "medium", "high"] = "none"
    matched_pattern: str = ""


def _match_any(patterns: list[str], text: str) -> str:
    for p in patterns:
        if re.search(p, text, flags=re.IGNORECASE):
            return p
    return ""


def detect_hostility_signal(msg: str) -> HostilitySignal:
    m = norm_text(msg)
    hard = _match_any(HARD_STOP_PATTERNS, m)
    if hard:
        return HostilitySignal(kind="hard_stop", confidence="high", matched_pattern=hard)
    high = _match_any(HIGH_HOSTILE_PATTERNS, m)
    if high:
        return HostilitySignal(kind="high_hostile", confidence="high", matched_pattern=high)
    medium = _match_any(MEDIUM_FRUSTRATION_PATTERNS, m)
    if medium:
        return HostilitySignal(kind="medium_frustration", confidence="medium", matched_pattern=medium)
    return HostilitySignal()


def looks_auto_reply(msg: str) -> bool:
    m = norm_text(msg)
    auto_patterns = [
        "thank you for contacting",
        "our team will respond shortly",
        "automated assistant",
        "auto reply",
        "we will get back",
    ]
    return any(p in m for p in auto_patterns)


def looks_action_intent(msg: str) -> bool:
    m = norm_text(msg)
    patterns = [
        "yes",
        "go ahead",
        "lets do it",
        "let's do it",
        "what's next",
        "whats next",
        "start",
        "proceed",
        "send it",
        "do it",
        "confirm",
    ]
    return any(p in m for p in patterns)


def extract_artifact_request(msg: str) -> dict[str, bool]:
    m = norm_text(msg)
    wants_abstract = any(p in m for p in ["abstract", "summary", "paper", "research note"])
    wants_draft = any(p in m for p in ["draft", "whatsapp", "message", "copy paste", "copy-paste"])
    wants_schedule = any(p in m for p in ["schedule", "tomorrow", "post at", "publish"])
    return {
        "wants_abstract": wants_abstract,
        "wants_draft": wants_draft,
        "wants_schedule": wants_schedule,
        "requested_any": wants_abstract or wants_draft or wants_schedule,
    }


def looks_off_topic(msg: str) -> bool:
    m = norm_text(msg)
    return any(p in m for p in ["gst", "income tax", "itr", "passport", "visa"])


def non_empty_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def find_active_offer(merchant: dict[str, Any]) -> Optional[str]:
    offers = merchant.get("offers", []) or []
    for offer in offers:
        if str(offer.get("status", "")).lower() == "active":
            title = offer.get("title")
            if non_empty_str(title):
                return title.strip()
    return None


def find_digest_item(category: dict[str, Any], item_id: str | None) -> Optional[dict[str, Any]]:
    if not item_id:
        return None
    for item in category.get("digest", []) or []:
        if item.get("id") == item_id:
            return item
    return None


def safe_body_or_none(body: str) -> Optional[str]:
    b = clip(body)
    if not b:
        return None
    if has_url(b):
        return None
    return b


def template_name_for(kind: str, send_as: str) -> str:
    prefix = "merchant" if send_as == "merchant_on_behalf" else "vera"
    clean = re.sub(r"[^a-z0-9_]", "_", (kind or "generic").lower())
    return f"{prefix}_{clean}_v1"


def _auto_reply_first_action(conv: "ConversationState") -> Literal["send", "wait"]:
    mode = str(os.getenv("AUTO_REPLY_FIRST_ACTION", "stage")).strip().lower()
    if mode in {"send", "wait"}:
        return mode  # explicit deterministic override
    # stage mode: early conversational stage nudges once, later stage backs off directly
    return "send" if conv.last_turn_number <= 2 else "wait"


def _artifact_reply_for_action_intent(conv: "ConversationState", msg: str) -> Optional[dict[str, str]]:
    req = extract_artifact_request(msg)
    if not req["requested_any"]:
        return None

    if not conv.trigger_id:
        return None
    trg_rec = contexts.get(("trigger", conv.trigger_id))
    m_rec = contexts.get(("merchant", conv.merchant_id))
    if not trg_rec or not m_rec:
        return None

    trigger = trg_rec.get("payload", {}) or {}
    merchant = m_rec.get("payload", {}) or {}
    cat_slug = merchant.get("category_slug")
    c_rec = contexts.get(("category", cat_slug)) if non_empty_str(cat_slug) else None
    category = c_rec.get("payload", {}) if c_rec else {}

    payload = trigger.get("payload", {}) or {}
    digest_id = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    item = find_digest_item(category, digest_id) if non_empty_str(digest_id) else None

    lines: list[str] = []

    if req["wants_abstract"]:
        if item:
            title = item.get("title", "current update")
            summary = item.get("summary")
            source = item.get("source")
            abstract = f"Abstract snapshot: {title}."
            if non_empty_str(summary):
                abstract += f" {summary}"
            if non_empty_str(source):
                abstract += f" Source: {source}."
            lines.append(clip(abstract, 320))
        else:
            lines.append("Abstract snapshot: I can share the key findings from this trigger in 3 bullet points.")

    if req["wants_draft"]:
        biz = merchant_name(merchant)
        if item and non_empty_str(item.get("title")):
            title = item.get("title", "the latest update")
            draft = (
                f'Draft message: "{title} is relevant for {biz} right now. '
                "If you want, we can share a short patient/customer note and next-step guidance today. Reply YES to finalize.\""
            )
        else:
            draft = (
                "Draft message: \"Quick update from our team: we have a practical action you can use today for this business signal. "
                "Reply YES and we will send it in final form.\""
            )
        lines.append(clip(draft, 320))

    if req["wants_schedule"]:
        lines.append("Scheduling option: I can queue this for tomorrow 10:00 AM local time. Reply CONFIRM to schedule.")

    if not lines:
        return None

    body = "Sharing requested items now.\n\n" + "\n\n".join(lines)
    safe = safe_body_or_none(body)
    if not safe:
        return None
    return {"body": safe, "cta": "binary_confirm_cancel"}


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    trigger_id: Optional[str]
    suppression_key: str
    created_at: str
    last_turn_number: int = 0
    auto_reply_count: int = 0
    hostility_score: int = 0
    last_hostility_kind: str = "none"
    ended: bool = False
    message_hashes: set[str] = field(default_factory=set)


@dataclass
class NudgeState:
    sent_count: int = 0
    engaged: bool = False
    closed: bool = False


class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int = Field(ge=1)
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


class HealthzResponse(BaseModel):
    status: str
    uptime_seconds: int
    contexts_loaded: dict[str, int]


class MetadataResponse(BaseModel):
    team_name: str
    team_members: list[str]
    model: str
    approach: str
    contact_email: str
    version: str
    submitted_at: str


class ContextAcceptedResponse(BaseModel):
    accepted: Literal[True]
    ack_id: str
    stored_at: str


class ContextRejectedResponse(BaseModel):
    accepted: Literal[False]
    reason: str
    current_version: int | None = None
    details: str | None = None


class TickAction(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: str | None = None
    send_as: Literal["vera", "merchant_on_behalf"]
    trigger_id: str
    template_name: str
    template_params: list[str]
    body: str
    cta: str
    suppression_key: str
    rationale: str


class TickResponse(BaseModel):
    actions: list[TickAction]


class ReplySendResponse(BaseModel):
    action: Literal["send"]
    body: str
    cta: str
    rationale: str


class ReplyWaitResponse(BaseModel):
    action: Literal["wait"]
    wait_seconds: int
    rationale: str


class ReplyEndResponse(BaseModel):
    action: Literal["end"]
    rationale: str


class TeardownResponse(BaseModel):
    ok: bool
    wiped_at: str


def _next_conv_id(merchant_id: str, trigger_id: str) -> str:
    global conv_counter
    conv_counter += 1
    return f"conv_{merchant_id}_{trigger_id}_{conv_counter:04d}"


def _nudge_key(merchant_id: str, customer_id: Optional[str], suppression_key: str, trigger_id: str) -> tuple[str, str, str]:
    return (merchant_id, customer_id or "", suppression_key or trigger_id)


def _conv_nudge_key(conv: ConversationState) -> tuple[str, str, str]:
    return _nudge_key(conv.merchant_id, conv.customer_id, conv.suppression_key, conv.trigger_id or conv.conversation_id)


def _close_conversation(conv: ConversationState) -> None:
    conv.ended = True
    ns = nudge_state.get(_conv_nudge_key(conv), NudgeState())
    ns.closed = True
    nudge_state[_conv_nudge_key(conv)] = ns


def _classify_cta(trigger: dict[str, Any], customer: Optional[dict[str, Any]]) -> str:
    kind = trigger.get("kind", "")
    if customer and kind in {"recall_due", "appointment_tomorrow", "trial_followup", "chronic_refill_due"}:
        return "multi_choice_slot"
    if kind in {"research_digest", "curious_ask_due"}:
        return "open_ended"
    return "binary_yes_no"


def _compose_research_like(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    owner = owner_name(merchant)
    payload = trigger.get("payload", {}) or {}

    digest_id = payload.get("top_item_id") or payload.get("digest_item_id")
    item = find_digest_item(category, digest_id)

    if item:
        title = item.get("title", "new update")
        source = item.get("source", "")
        source_part = f" — {source}" if non_empty_str(source) else ""
        body = (
            f"{owner}, quick update: {title}. "
            f"Want me to draft a 2-line message you can send today?{source_part}"
        )
        rationale = f"{kind} trigger mapped to category digest item with source-grounded specificity."
        return body, rationale

    body = (
        f"{owner}, quick update from this {human_kind(kind)} trigger. "
        "Want me to draft a concise message you can use right away?"
    )
    rationale = "Research/compliance-style trigger without resolvable digest item; kept generic and grounded."
    return body, rationale


def _compose_perf_like(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    metric = payload.get("metric")
    delta_pct = payload.get("delta_pct")

    if non_empty_str(metric) and isinstance(delta_pct, (int, float)):
        direction = "up" if delta_pct > 0 else "down"
        body = (
            f"{owner}, quick signal: your {metric} is {direction} {pct(abs(float(delta_pct)))} on this cycle. "
            "Want me to draft a focused next-step plan for today?"
        )
        rationale = "Used explicit metric and delta from trigger payload."
        return body, rationale

    perf = merchant.get("performance", {}) or {}
    ctr = perf.get("ctr")
    views = perf.get("views")
    calls = perf.get("calls")
    facts = []
    if isinstance(views, (int, float)):
        facts.append(f"views {int(views)}")
    if isinstance(calls, (int, float)):
        facts.append(f"calls {int(calls)}")
    if isinstance(ctr, (int, float)):
        facts.append(f"CTR {pct(ctr)}")
    fact_str = ", ".join(facts) if facts else "current performance state"

    body = (
        f"{owner}, I can see a {human_kind(kind)} signal for your listing ({fact_str}). "
        "Want me to suggest one practical action for this week?"
    )
    rationale = "No exact trigger delta available; used merchant performance facts without invented percentages."
    return body, rationale


def _compose_customer_recall(
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any],
) -> tuple[str, str]:
    payload = trigger.get("payload", {}) or {}
    m_name = merchant_name(merchant)
    c_name = customer.get("identity", {}).get("name") or "there"
    offer = find_active_offer(merchant)
    service_due = payload.get("service_due")
    due_date = payload.get("due_date")
    slots = payload.get("available_slots", []) or []

    slot_labels = []
    for slot in slots[:2]:
        label = slot.get("label")
        if non_empty_str(label):
            slot_labels.append(label.strip())

    slot_text = ""
    cta = "open_ended"
    if len(slot_labels) >= 2:
        slot_text = f"Slots available: {slot_labels[0]} or {slot_labels[1]}."
        cta = "multi_choice_slot"
    elif len(slot_labels) == 1:
        slot_text = f"Slot available: {slot_labels[0]}."
        cta = "multi_choice_slot"
    else:
        slot_text = "Tell us your preferred time and we will confirm."

    due_text = f"Your {service_due} is due." if non_empty_str(service_due) else "Your follow-up is due."
    if non_empty_str(due_date):
        due_text += f" Due date: {due_date}."

    offer_text = f" {offer} is currently active." if offer else ""

    body = f"Hi {c_name}, {m_name} here. {due_text} {slot_text}{offer_text} Reply with your preferred option."
    rationale = "Customer-scoped follow-up using trigger due data + available slots + merchant active offer when present."
    return body, rationale + f" CTA={cta}."


def _compose_supply_alert(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    
    batches = payload.get("batch_ids", payload.get("batches", []))
    mfr = payload.get("manufacturer", payload.get("mfr", "the manufacturer"))
    topic = payload.get("topic", payload.get("molecule", "product recall"))
    affected = payload.get("affected_customer_count", payload.get("impacted_count"))
    
    batch_str = f" (batches: {', '.join(batches[:2])})" if batches else ""
    affected_str = f" I've identified {affected} of your repeat-Rx customers potentially impacted." if affected else ""
    
    body = (
        f"{owner}, urgent supply alert: {topic} recall by {mfr}{batch_str}.{affected_str} "
        "Want me to draft the patient notification and replacement workflow for you?"
    )
    rationale = f"Handled {kind} with specific batch/mfr/impact data from payload."
    return body, rationale


def _compose_milestone(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    
    milestone = payload.get("milestone_label", payload.get("milestone", "a new milestone"))
    metric = payload.get("metric_name", "")
    value = payload.get("current_value", "")
    
    fact_str = f" - you've reached {value} {metric}!" if value and metric else "!"
    body = (
        f"Congratulations {owner}! {milestone}{fact_str} This is great for your social proof. "
        "Want me to draft a 'Thank You' post for your Google profile and Instagram?"
    )
    rationale = f"Personalized {kind} with specific milestone values."
    return body, rationale


def _compose_review_theme(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    
    theme = payload.get("theme", "recent reviews")
    sentiment = payload.get("sentiment", "positive")
    count = payload.get("review_count", "")
    
    body = (
        f"{owner}, I noticed a {sentiment} theme in your {count if count else 'latest'} reviews regarding '{theme}'. "
        "Want me to draft a reply that highlights this strength to new customers?"
    )
    rationale = f"Addressed {kind} by anchoring on the specific '{theme}' identified in reviews."
    return body, rationale


def _compose_festival(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    
    festival = payload.get("festival_name", "the upcoming festival")
    date = payload.get("date", "")
    
    date_str = f" on {date}" if date else ""
    body = (
        f"Hi {owner}, {festival} is coming up{date_str}. It's a great time to engage your regulars with a festive offer. "
        "Want me to draft a greetings post + a special discount story for you?"
    )
    rationale = f"Handled {kind} by anchoring on the specific festival name and date."
    return body, rationale


def _compose_curious_ask(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    owner = owner_name(merchant)
    biz = merchant_name(merchant)
    
    body = (
        f"Hi {owner}! Quick check — what service or product has been most asked-for this week at {biz}? "
        "I'll turn your answer into a Google post + a 4-line WhatsApp reply you can use for customer queries. Takes 2 min."
    )
    rationale = f"Handled {kind} with a curiosity-driven reciprocity hook (effort externalization)."
    return body, rationale


def _compose_ipl(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    
    match = payload.get("match_label", payload.get("match", "the IPL match"))
    time = payload.get("match_time", "")
    venue = payload.get("venue", "")
    
    venue_str = f" at {venue}" if venue else ""
    time_str = f", {time}" if time else ""
    body = (
        f"Quick heads-up {owner} — {match}{venue_str}{time_str}. Match nights can shift footfall; "
        "want me to draft a match-night delivery special or a 'watch-party' offer to keep orders high?"
    )
    rationale = f"Handled {kind} by anchoring on the specific match details and offering relevant conversion hooks."
    return body, rationale


def _compose_competitor(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    
    comp = payload.get("competitor_name", "a new competitor")
    dist = payload.get("distance_km", payload.get("distance", ""))
    
    dist_str = f" just {dist}km away" if dist else " nearby"
    body = (
        f"{owner}, I noticed {comp} has opened{dist_str}. It's important to keep your regulars engaged now. "
        "Want me to draft a 'Loyalty Appreciation' WhatsApp to your top customers today?"
    )
    rationale = f"Handled {kind} with a social-proof/competition hook to drive defensive engagement."
    return body, rationale


def _compose_generic(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "update")
    owner = owner_name(merchant)
    active_offer = find_active_offer(merchant)
    offer_line = f" You already have {active_offer} active." if active_offer else ""
    body = (
        f"{owner}, quick check: I see a {human_kind(kind)} signal for your business.{offer_line} "
        "Want me to draft the exact message you can send today?"
    )
    rationale = "Fallback path for sparse payload; grounded to trigger kind + merchant context."
    return body, rationale


def compose(
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind = trigger.get("kind", "")
    scope = trigger.get("scope", "merchant")
    send_as = "merchant_on_behalf" if scope == "customer" else "vera"

    if scope == "customer" and customer:
        if kind in {"recall_due", "appointment_tomorrow", "trial_followup", "chronic_refill_due", "customer_lapsed_hard", "customer_lapsed_soft", "wedding_package_followup"}:
            body, rationale = _compose_customer_recall(merchant, trigger, customer)
        else:
            body, rationale = _compose_generic(category, merchant, trigger)
    elif kind in {"research_digest", "regulation_change", "cde_opportunity"}:
        body, rationale = _compose_research_like(category, merchant, trigger)
    elif kind == "supply_alert":
        body, rationale = _compose_supply_alert(merchant, trigger)
    elif kind in {"perf_dip", "perf_spike", "seasonal_perf_dip"}:
        body, rationale = _compose_perf_like(merchant, trigger)
    elif kind == "milestone_reached":
        body, rationale = _compose_milestone(merchant, trigger)
    elif kind == "review_theme_emerged":
        body, rationale = _compose_review_theme(merchant, trigger)
    elif kind == "festival_upcoming":
        body, rationale = _compose_festival(category, merchant, trigger)
    elif kind == "curious_ask_due":
        body, rationale = _compose_curious_ask(category, merchant, trigger)
    elif kind == "ipl_match_today":
        body, rationale = _compose_ipl(category, merchant, trigger)
    elif kind == "competitor_opened":
        body, rationale = _compose_competitor(category, merchant, trigger)
    elif kind == "active_planning_intent":
        owner = owner_name(merchant)
        last_msg = trigger.get("payload", {}).get("merchant_last_message")
        body = (
            f"{owner}, let's move this to execution. I can draft the first version now and keep it editable. "
            f"{'Context from your last note: ' + str(last_msg) + '. ' if non_empty_str(last_msg) else ''}"
            "Reply YES and I will send the ready-to-use draft."
        )
        rationale = "Intent transition trigger routed to action mode with immediate next artifact."
    else:
        body, rationale = _compose_generic(category, merchant, trigger)

    pref = language_pref(customer, merchant)
    if "hi" in pref and "reply" in body.lower():
        body = body.replace("Reply", "Reply / jawab")

    safe = safe_body_or_none(body)
    if not safe:
        safe = "Quick update available for your account. Want me to draft the next step?"

    cta = _classify_cta(trigger, customer)
    if scope == "customer" and trigger.get("kind") in {"recall_due", "appointment_tomorrow", "trial_followup", "chronic_refill_due"}:
        cta = "multi_choice_slot"

    return {
        "body": safe,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", "") or f"{trigger.get('id', 'unknown')}:fallback",
        "rationale": clip(rationale, 260),
    }


def _validate_action_payload(action: dict[str, Any], conv: ConversationState) -> Optional[str]:
    required = ["conversation_id", "merchant_id", "send_as", "trigger_id", "cta", "suppression_key", "rationale", "body", "template_name", "template_params"]
    for key in required:
        if key not in action:
            return f"missing_{key}"
    if not non_empty_str(action.get("body")):
        return "empty_body"
    if has_url(action["body"]):
        return "url_not_allowed"
    if action["send_as"] not in {"vera", "merchant_on_behalf"}:
        return "invalid_send_as"
    h = text_hash(action["body"])
    if h in conv.message_hashes:
        return "repeated_body"
    return None


@app.get("/v1/healthz", response_model=HealthzResponse)
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _ctx_id), _rec in contexts.items():
        if scope in counts:
            counts[scope] += 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TS),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata", response_model=MetadataResponse)
async def metadata():
    return {
        "team_name": "Codex Implementation",
        "team_members": ["Codex"],
        "model": "rule_based_v1",
        "approach": "deterministic context-grounded composer + reply policy state machine",
        "contact_email": "na@example.com",
        "version": "0.1.0",
        "submitted_at": "2026-04-30T00:00:00Z",
    }


@app.post("/v1/context", response_model=Union[ContextAcceptedResponse, ContextRejectedResponse])
async def push_context(body: CtxBody, response: Response):
    scope = (body.scope or "").strip()
    if scope not in ALLOWED_SCOPES:
        response.status_code = 400
        return {"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {sorted(ALLOWED_SCOPES)}"}

    key = (scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        response.status_code = 409
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}

    contexts[key] = {
        "version": body.version,
        "payload": body.payload,
        "delivered_at": body.delivered_at,
        "stored_at": utc_now_iso(),
    }
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": utc_now_iso()}


@app.post("/v1/tick", response_model=TickResponse)
async def tick(body: TickBody):
    now = parse_dt(body.now) or utc_now()
    actions: list[dict[str, Any]] = []
    tick_started = time.monotonic()
    tick_guard_deadline_s = 9.0
    seen_pairs: set[tuple[str, str]] = set()

    for trg_id in body.available_triggers:
        if (time.monotonic() - tick_started) > tick_guard_deadline_s:
            break
        if len(actions) >= 20:
            break
        trg_rec = contexts.get(("trigger", trg_id))
        if not trg_rec:
            continue
        trigger = trg_rec["payload"]
        exp = parse_dt(trigger.get("expires_at"))
        if exp and now > exp:
            continue

        merchant_id = trigger.get("merchant_id")
        if not non_empty_str(merchant_id):
            continue
        m_rec = contexts.get(("merchant", merchant_id))
        if not m_rec:
            continue
        merchant = m_rec["payload"]
        cat_slug = merchant.get("category_slug")
        c_rec = contexts.get(("category", cat_slug)) if non_empty_str(cat_slug) else None
        if not c_rec:
            continue
        category = c_rec["payload"]

        customer = None
        customer_id = trigger.get("customer_id")
        if trigger.get("scope") == "customer":
            if not non_empty_str(customer_id):
                continue
            cust_rec = contexts.get(("customer", customer_id))
            if not cust_rec:
                continue
            customer = cust_rec["payload"]

        suppression_key = trigger.get("suppression_key") or f"{trg_id}:fallback"
        s_key = (merchant_id, customer_id or "", suppression_key)
        if s_key in suppression_sent:
            continue

        n_key = _nudge_key(merchant_id, customer_id, suppression_key, trg_id)
        ns = nudge_state.get(n_key, NudgeState())
        if ns.sent_count >= 3 and not ns.engaged:
            ns.closed = True
            nudge_state[n_key] = ns
            continue
        if ns.closed:
            continue

        composed = compose(category, merchant, trigger, customer)
        conv_id = _next_conv_id(merchant_id, trg_id)
        conv = ConversationState(
            conversation_id=conv_id,
            merchant_id=merchant_id,
            customer_id=customer_id if non_empty_str(customer_id) else None,
            trigger_id=trg_id,
            suppression_key=composed["suppression_key"],
            created_at=utc_now_iso(),
        )

        headline = clip(composed["body"], 160)
        action = {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id if non_empty_str(customer_id) else None,
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": template_name_for(trigger.get("kind", "generic"), composed["send_as"]),
            "template_params": [
                owner_name(merchant),
                headline,
                human_kind(trigger.get("kind", "update")),
            ],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        }

        pair = (action["merchant_id"], action["conversation_id"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        err = _validate_action_payload(action, conv)
        if err:
            continue

        conv.message_hashes.add(text_hash(action["body"]))
        conversations[conv_id] = conv
        suppression_sent[s_key] = utc_now_iso()
        ns.sent_count += 1
        nudge_state[n_key] = ns
        actions.append(action)

    return {"actions": actions}


@app.post("/v1/reply", response_model=Union[ReplySendResponse, ReplyWaitResponse, ReplyEndResponse])
async def reply(body: ReplyBody):
    conv = conversations.get(body.conversation_id)
    if not conv:
        conv = ConversationState(
            conversation_id=body.conversation_id,
            merchant_id=body.merchant_id or "unknown",
            customer_id=body.customer_id,
            trigger_id=None,
            suppression_key=f"conv:{body.conversation_id}",
            created_at=utc_now_iso(),
        )
        conversations[body.conversation_id] = conv

    conv.last_turn_number = max(conv.last_turn_number, body.turn_number)

    msg = body.message or ""
    if conv.ended:
        return {"action": "end", "rationale": "Conversation already closed."}

    signal = detect_hostility_signal(msg)
    if signal.kind == "hard_stop":
        _close_conversation(conv)
        return {"action": "end", "rationale": "Detected explicit opt-out/not-interested signal; closing immediately."}

    if signal.kind == "high_hostile":
        conv.hostility_score += 2
        conv.last_hostility_kind = signal.kind
        _close_conversation(conv)
        return {"action": "end", "rationale": "Detected high-confidence hostile signal; closing gracefully to avoid escalation."}

    if signal.kind == "medium_frustration":
        conv.hostility_score += 1
        conv.last_hostility_kind = signal.kind
        if conv.hostility_score >= 2:
            _close_conversation(conv)
            return {"action": "end", "rationale": "Repeated frustration signals detected; ending conversation respectfully."}
        out = "Sorry for the disturbance. I’ll keep this brief. If you prefer no further messages, reply STOP."
        out = safe_body_or_none(out) or "Sorry for the disturbance. Reply STOP if you want no further messages."
        h = text_hash(out)
        if h in conv.message_hashes:
            out = "Understood. I’ll pause unless you want help. Reply STOP to opt out."
            h = text_hash(out)
        conv.message_hashes.add(h)
        return {"action": "send", "body": out, "cta": "binary_yes_no", "rationale": "Medium-confidence frustration detected; sent one de-escalation step."}

    if looks_auto_reply(msg):
        conv.auto_reply_count += 1
        if conv.auto_reply_count == 1:
            first_action = _auto_reply_first_action(conv)
            if first_action == "wait":
                wait_seconds = env_int("AUTO_REPLY_WAIT_SECONDS", 14400)
                return {"action": "wait", "wait_seconds": max(300, wait_seconds), "rationale": "Detected canned auto-reply; policy selected immediate backoff for first occurrence."}
            out = "Looks like an auto-reply. When the owner is available, reply YES and I will continue from here."
            h = text_hash(out)
            if h in conv.message_hashes:
                out = "Looks like an automated reply. Ask the owner to reply YES when free."
                h = text_hash(out)
            conv.message_hashes.add(h)
            return {"action": "send", "body": out, "cta": "binary_yes_no", "rationale": "Detected canned auto-reply; policy selected one owner-directed nudge on first occurrence."}
        if conv.auto_reply_count == 2:
            wait_seconds = env_int("AUTO_REPLY_WAIT_SECONDS", 14400)
            return {"action": "wait", "wait_seconds": max(300, wait_seconds), "rationale": "Repeated auto-reply; backing off before retry."}
        _close_conversation(conv)
        return {"action": "end", "rationale": "Auto-reply repeated 3 times with no engagement; ending conversation."}

    # Any non-auto inbound is meaningful engagement
    n_key = _conv_nudge_key(conv)
    ns = nudge_state.get(n_key, NudgeState())
    ns.engaged = True
    nudge_state[n_key] = ns

    if looks_action_intent(msg):
        artifact_reply = _artifact_reply_for_action_intent(conv, msg)
        if artifact_reply:
            h = text_hash(artifact_reply["body"])
            if h not in conv.message_hashes:
                conv.message_hashes.add(h)
                return {
                    "action": "send",
                    "body": artifact_reply["body"],
                    "cta": artifact_reply["cta"],
                    "rationale": "Detected explicit commitment plus concrete artifact request; returned requested artifacts in the same turn.",
                }
        out = "Great. Moving to action now: I can draft the exact next message and checklist in one go. Reply CONFIRM to proceed."
        out = safe_body_or_none(out) or "Great. Moving to action now. Reply CONFIRM to proceed."
        h = text_hash(out)
        if h in conv.message_hashes:
            out = "Perfect. I will execute the next step now. Reply CONFIRM and I’ll send the ready draft."
            h = text_hash(out)
        conv.message_hashes.add(h)
        return {"action": "send", "body": out, "cta": "binary_confirm_cancel", "rationale": "Detected explicit commitment; switched from qualification to action mode."}

    if looks_off_topic(msg):
        out = "I should leave GST/CA work to your accountant. On this thread, I can help with the current business trigger. Want me to continue?"
        out = safe_body_or_none(out) or "I can only help with this current business thread. Want me to continue?"
        h = text_hash(out)
        if h in conv.message_hashes:
            out = "Out of scope for me, but I can continue with this current business action. Continue?"
            h = text_hash(out)
        conv.message_hashes.add(h)
        return {"action": "send", "body": out, "cta": "open_ended", "rationale": "Polite out-of-scope handling with redirect to active context."}

    out = "Understood. I can take this forward and send a concise next-step draft now. Want that?"
    out = safe_body_or_none(out) or "Understood. Want me to send the next-step draft?"
    h = text_hash(out)
    if h in conv.message_hashes:
        out = "Got it. Want me to send a short actionable draft for this?"
        h = text_hash(out)
    conv.message_hashes.add(h)
    return {"action": "send", "body": out, "cta": "open_ended", "rationale": "Acknowledged response and advanced toward a concrete next step."}


@app.post("/v1/teardown", response_model=TeardownResponse)
async def teardown():
    contexts.clear()
    conversations.clear()
    suppression_sent.clear()
    nudge_state.clear()
    global conv_counter
    conv_counter = 0
    return {"ok": True, "wiped_at": utc_now_iso()}
