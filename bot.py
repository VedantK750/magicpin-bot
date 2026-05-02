import hashlib
import os
import re
import time
import asyncio
import unicodedata
from functools import partial
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Union

from fastapi import FastAPI, Response
from pydantic import BaseModel, Field

import llm_composer


app = FastAPI(title="magicpin-challenge-bot", version="0.1.0")
START_TS = time.time()

ALLOWED_SCOPES = {"category", "merchant", "customer", "trigger"}
URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)

# Global in-memory state
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, "ConversationState"] = {}
suppression_sent: dict[tuple[str, str, str], str] = {}
nudge_state: dict[tuple[str, str, str], "NudgeState"] = {}
global_auto_reply_count: dict[tuple[str, str], int] = {}
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


def clip(text: str, limit: int = 2000) -> str:
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
    name = merchant.get("identity", {}).get("owner_first_name") or merchant_name(merchant)
    if merchant.get("category_slug") == "dentists" and not name.startswith("Dr."):
        return f"Dr. {name}"
    return name


def language_pref(customer: Optional[dict[str, Any]], merchant: dict[str, Any]) -> str:
    if customer:
        lp = customer.get("identity", {}).get("language_pref")
        if isinstance(lp, str) and lp:
            return lp.lower()
    
    langs = merchant.get("identity", {}).get("languages", [])
    # Prioritize non-English, non-Hindi languages first for better regional fit
    other_langs = [l.lower() for l in langs if l.lower() not in {"en", "hi"}]
    if other_langs:
        return f"{other_langs[0]}-en mix"
    
    # Fallback to Hindi if present
    if "hi" in [l.lower() for l in langs]:
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
    history: list[dict[str, str]] = field(default_factory=list) # [{'role': 'vera'|'user', 'msg': str}]


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
    biz = merchant_name(merchant)
    loc = merchant.get("identity", {}).get("locality", "")
    loc_str = f" in {loc}" if loc else ""
    payload = trigger.get("payload", {}) or {}

    digest_id = payload.get("top_item_id") or payload.get("digest_item_id")
    item = find_digest_item(category, digest_id)

    if item:
        title = item.get("title", "new update")
        body = (
            f"{owner}, quick update for {biz}{loc_str}: {title}. "
            "Want me to draft a 2-line message you can send today to your customers?"
        )
        rationale = f"{kind} trigger mapped to category digest item with source-grounded specificity."
        return body, rationale

    body = (
        f"{owner}, quick update for {biz} from this {human_kind(kind)} trigger. "
        "Want me to draft a concise message you can use right away?"
    )
    rationale = "Research/compliance-style trigger without resolvable digest item; kept generic and grounded."
    return body, rationale


def _compose_perf_like(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    biz = merchant_name(merchant)
    metric = payload.get("metric", "engagement")
    delta_pct = payload.get("delta_pct") or 0.0
    
    perf = merchant.get("performance", {}) or {}
    window = payload.get("window", perf.get("window_days", 7))
    current_val = perf.get(metric, "")

    body = (
        f"{owner}, quick signal for {biz}: your {metric} are {'up' if float(delta_pct) >= 0 else 'down'} {pct(abs(float(delta_pct)))} over the last {window} days. "
        f"(Exact current value: {current_val}). (Source: magicpin performance analytics). "
        "Want me to analyze the source of this shift and suggest a recovery plan?"
    )
    rationale = f"Performance signal: {metric} {delta_pct} over {window}."
    return body, rationale


def _compose_customer_recall(
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any],
) -> tuple[str, str]:
    payload = trigger.get("payload", {}) or {}
    m_name = merchant_name(merchant)
    c_name_raw = customer.get("identity", {}).get("name") or "there"
    
    # Clean up "Name (parent: Parent)" formatting
    c_name = c_name_raw
    p_name = ""
    if "(" in c_name_raw and "parent:" in c_name_raw.lower():
        parts = c_name_raw.split("(")
        c_name = parts[0].strip()
        p_name = parts[1].replace("parent:", "").replace(")", "").strip()
    
    offer = find_active_offer(merchant)
    service_due = payload.get("service_due") or payload.get("topic", "follow-up visit")
    due_date = payload.get("due_date")
    slots = payload.get("available_slots", []) or []
    
    # Extract facts for the LLM
    last_v = payload.get("last_visit") or payload.get("days_since_last_visit")
    goal = payload.get("previous_focus") or payload.get("goal")
    wedding = payload.get("wedding_date")
    days_to_wedding = payload.get("days_to_wedding")
    
    slot_labels = []
    for slot in slots[:2]:
        label = slot.get("label")
        if non_empty_str(label):
            slot_labels.append(label.strip())

    slot_text = ""
    if slot_labels:
        if len(slot_labels) >= 2:
            slot_text = f"We have slots available: {slot_labels[0]} or {slot_labels[1]}."
        else:
            slot_text = f"We have a slot available: {slot_labels[0]}."
    else:
        slot_text = "Tell us your preferred time and we will confirm it for you."

    # Addressing logic (Parent vs Customer)
    greet_name = p_name if p_name else c_name
    child_part = f" for {c_name}" if p_name else ""

    # Safe sentence for fallback
    wedding_info = f" ({days_to_wedding} days to your big day on {wedding})" if wedding and days_to_wedding else ""
    body = f"Hi {greet_name}, {m_name} here. It's time for the next {service_due.replace('_', ' ')}{child_part}{wedding_info}. {slot_text}"
    if non_empty_str(due_date):
        body += f" (Target date: {due_date})"
    
    if offer:
        body += f" Also, our {offer} is currently active."

    body += " Reply with your preferred option and we'll handle the rest!"
    
    rationale = f"Customer follow-up for {service_due} anchored on payload facts."
    return body, rationale


def _compose_supply_alert(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    biz = merchant_name(merchant)

    batches = payload.get("affected_batches", payload.get("batch_ids", payload.get("batches", [])))
    mfr = payload.get("manufacturer", payload.get("mfr", "the manufacturer"))
    topic = payload.get("molecule", payload.get("topic", "product recall"))
    affected = payload.get("affected_customer_count", payload.get("impacted_count"))

    batch_str = f" (batches: {', '.join(batches[:2])})" if batches else ""
    affected_str = f" I've identified {affected} of your repeat customers potentially impacted." if affected else " A portion of your repeat customers may be impacted."

    body = (
        f"{owner}, urgent supply alert for {biz}: {topic} recall by {mfr}{batch_str}.{affected_str} "
        "Want me to draft the patient notification and replacement workflow for you?"
    )
    rationale = f"Handled {kind} with factual cohort impact based on payload."
    return body, rationale

def _compose_milestone(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    biz = merchant_name(merchant)

    milestone_val = payload.get("milestone_value", payload.get("milestone", "100"))
    metric = payload.get("metric_name", payload.get("metric", "engagement"))
    value = payload.get("current_value", payload.get("value_now", ""))

    metric_map = {"review_count": "Google reviews", "views": "profile views", "calls": "customer calls"}
    metric_label = metric_map.get(metric, str(metric).replace("_", " "))

    diff = int(milestone_val) - int(value) if value and milestone_val else 5

    perf = merchant.get("performance", {}) or {}
    total_views = perf.get("views", 0)

    fact_str = f" - {biz} just reached {value} {metric_label}! (Total {total_views} views YTD) [Source: magicpin performance data]" if value and metric_label else "!"
    body = (
        f"Congratulations {owner}! You're just {diff} {metric_label} away from the {milestone_val} milestone{fact_str} "
        "This social proof is great for attracting new customers. Want me to draft a 'Thank You' post?"
    )
    rationale = f"Personalized {kind} with precise {metric_label} countdown and source citation."
    return body, rationale

def _compose_review_theme(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    biz = merchant_name(merchant)
    
    theme = payload.get("theme", "recent reviews")
    sentiment = str(payload.get("sentiment", "negative" if "late" in theme or "bad" in theme or "slow" in theme else "positive")).lower()
    count = payload.get("occurrences_30d", payload.get("review_count", ""))
    quote = payload.get("common_quote")
    
    perf = merchant.get("performance", {}) or {}
    rating = merchant.get("performance", {}).get("avg_rating", "4.0+")
    
    count_str = f" across {count} recent reviews" if count else ""
    quote_str = f' (e.g., "{quote}")' if quote else ""
    
    if sentiment == "positive":
        body = (
            f"{owner}, I noticed a positive theme{count_str} regarding '{theme}'{quote_str} for {biz}. "
            f"Your rating is strong at {rating}. Want me to draft a reply that highlights this strength to new customers?"
        )
        rationale = f"Anchored on positive review theme '{theme}' + merchant rating."
    else:
        body = (
            f"{owner}, a few reviews{count_str} mentioned '{theme}'{quote_str} for {biz}. "
            "Want me to draft a professional response to address this and keep your rating high?"
        )
        rationale = f"Anchored on negative review theme '{theme}' with professional recovery framing."
    return body, rationale


def _compose_festival(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    biz = merchant_name(merchant)

    festival = payload.get("festival", payload.get("festival_name", "the upcoming festival"))
    date = payload.get("date", "")
    offer = find_active_offer(merchant)
    offer_str = f" (your {offer} is perfect for this)." if offer else ""

    date_str = f" on {date}" if date else ""
    body = (
        f"Hi {owner}, {festival} is coming up{date_str} for {biz}.{offer_str} It's a great time to engage your regulars with a festive offer. "
        "Want me to draft a greetings post + a 10% 'Festive Flash' offer for your regulars?"
    )
    rationale = f"Handled {kind} by anchoring on '{festival}' and active offer."
    return body, rationale

def _compose_curious_ask(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    owner = owner_name(merchant)
    biz = merchant_name(merchant)

    perf = merchant.get("performance", {}) or {}
    views = perf.get("views", 0)
    calls = perf.get("calls", 0)

    context_str = ""
    if views > 0 or calls > 0:
        context_str = f" With {views} views and {calls} calls recently, I want to keep your momentum high."

    body = (
        f"Hi {owner}! Quick check — what service or product has been most asked-for this week at {biz}?{context_str} "
        "Tell me one item and I'll turn it into a Google post for you."
    )
    rationale = f"Handled {kind} with performance anchoring ({views} views) and a curiosity-driven reciprocity hook."
    return body, rationale

def _compose_ipl(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    biz = merchant_name(merchant)
    
    match = payload.get("match", payload.get("match_label", "the IPL match"))
    venue = payload.get("venue", "the stadium")
    
    body = (
        f"Quick heads-up {owner} — {match} at {venue} tonight. Match nights can shift footfall; "
        "want me to draft a match-night delivery special or a 'watch-party' offer to keep orders high?"
    )
    rationale = f"Handled {kind} by anchoring on the specific match details."
    return body, rationale


def _compose_competitor(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    owner = owner_name(merchant)
    
    comp = payload.get("competitor_name", "a new competitor")
    dist = payload.get("distance_km", payload.get("distance", ""))
    offer = payload.get("their_offer")
    
    dist_str = f" just {dist}km away" if dist else " nearby"
    offer_str = f" They are running a {offer}." if offer else ""
    
    body = (
        f"{owner}, I noticed {comp} has opened{dist_str}.{offer_str} It's important to keep your regulars engaged now. "
        "Want me to draft a 'Loyalty Appreciation' WhatsApp to your top customers today?"
    )
    rationale = f"Handled {kind} with a competition hook including their offer."
    return body, rationale


def _compose_gbp_unverified(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    owner = owner_name(merchant)
    biz = merchant_name(merchant)
    payload = trigger.get("payload", {}) or {}
    uplift = payload.get("estimated_uplift_pct")
    uplift_str = f" It could boost your visibility by {pct(uplift)}!" if uplift else ""
    
    perf = merchant.get("performance", {}) or {}
    calls = perf.get("calls", 0)
    
    body = (
        f"{owner}, I noticed your Google Business Profile for {biz} isn't fully verified yet.{uplift_str} "
        f"With {calls} calls last month, verification could significantly increase your reach. "
        "Want me to guide you through the 2-minute setup?"
    )
    rationale = "Verification hook with estimated uplift + current calls context."
    return body, rationale


def _compose_dormant(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    owner = owner_name(merchant)
    biz = merchant_name(merchant)
    payload = trigger.get("payload", {}) or {}
    days = payload.get("days_since_last_merchant_message")
    
    perf = merchant.get("performance", {}) or {}
    views = perf.get("views", 0)
    
    days_str = f"It's been {days} days since we last updated your profile for {biz}." if days else f"It's been a while since our last update for {biz}."
    
    body = (
        f"Hi {owner}, {days_str} Your listing is still getting {views} views, but fresh content could help convert them. "
        "Want me to suggest a quick update for this week?"
    )
    rationale = "Dormancy re-engagement with specific view count context."
    return body, rationale


def _compose_winback(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    owner = owner_name(merchant)
    biz = merchant_name(merchant)
    payload = trigger.get("payload", {}) or {}
    lapsed = payload.get("lapsed_customers_added_since_expiry")
    lapsed_str = f" {lapsed} of your regulars haven't visited in a while." if lapsed else ""
    
    perf = merchant.get("performance", {}) or {}
    ctr = perf.get("ctr", 0)
    
    body = (
        f"{owner}, it might be a good time to restart your growth plan for {biz}.{lapsed_str} "
        f"With your current CTR at {pct(ctr)}, we have a great baseline to improve. "
        "Want me to draft a 'welcome back' offer to re-engage them today?"
    )
    rationale = "Winback composer focusing on lapsed customer opportunity + performance baseline."
    return body, rationale


def _compose_renewal(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    owner = owner_name(merchant)
    biz = merchant_name(merchant)
    payload = trigger.get("payload", {}) or {}
    days = payload.get("days_remaining")
    plan = payload.get("plan", "Pro")
    
    perf = merchant.get("performance", {}) or {}
    views = perf.get("views", 0)
    
    days_str = f" in {days} days" if days else " soon"
    perf_str = f" (your profile hit {views} views this month!)" if views else ""
    
    body = (
        f"Hi {owner}, {biz}'s {plan} plan is set to renew{days_str}{perf_str}. "
        "I've prepared a summary of your wins this month to help you decide. Want me to send it?"
    )
    rationale = "Renewal trigger with achievement-summary and performance teaser."
    return body, rationale


def _compose_category_seasonal(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    owner = owner_name(merchant)
    biz = merchant_name(merchant)
    payload = trigger.get("payload", {}) or {}
    season = payload.get("season", "the current season").replace("_", " ")
    trends = payload.get("trends", [])
    
    trend_str = ""
    if trends and isinstance(trends, list):
        clean_trends = []
        for t in trends[:2]:
            t_clean = t.replace("_", " ").replace("+", "up ").replace("-", "down ")
            if "up " in t_clean or "down " in t_clean:
                # Ensure units are visible
                parts = t_clean.split(" ")
                if parts[-1].isdigit():
                    t_clean += "%"
            clean_trends.append(t_clean)
        trend_str = f" (seeing {', '.join(clean_trends)})."
    
    body = (
        f"Hi {owner}, we're seeing some interesting seasonal shifts for {biz} this {season}{trend_str}. "
        "Want me to draft a quick post to align your listing with what customers are searching for right now?"
    )
    rationale = f"Unpacked seasonal trends with units: {season}."
    return body, rationale


def _compose_generic(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "update")
    owner = owner_name(merchant)
    biz = merchant_name(merchant)
    loc = merchant.get("identity", {}).get("locality", "")
    loc_str = f" in {loc}" if loc else ""
    
    active_offer = find_active_offer(merchant)
    offer_line = f" You already have {active_offer} active." if active_offer else ""
    
    # Avoid jargon in generic fallback
    kind_label = human_kind(kind)
    if any(j in kind_label for j in ["signal", "dormant", "unverified"]):
        kind_label = "new update"
        
    body = (
        f"{owner}, quick check for {biz}{loc_str}: I see a {kind_label} for your business.{offer_line} "
        "Want me to draft the exact message you can send today?"
    )
    rationale = "Fallback path for sparse payload; jargon-safe with locality/biz name."
    return body, rationale


def format_data_block(data: dict[str, Any], prefix: str = "Data") -> str:
    """Helper to format a dictionary into a readable context string for the LLM."""
    if not data:
        return ""
    items = []
    for k, v in data.items():
        if v is not None and not isinstance(v, (dict, list)):
            items.append(f"{k}={v}")
    return f" [{prefix}: {', '.join(items)}]" if items else ""


async def compose(
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind = trigger.get("kind", "")
    scope = trigger.get("scope", "merchant")
    send_as = "merchant_on_behalf" if scope == "customer" else "vera"

    # --- Step 1: Fact Extraction (Rule-Based) ---
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
    elif kind == "gbp_unverified":
        body, rationale = _compose_gbp_unverified(merchant, trigger)
    elif kind == "dormant_with_vera":
        body, rationale = _compose_dormant(merchant, trigger)
    elif kind == "winback_eligible":
        body, rationale = _compose_winback(merchant, trigger)
    elif kind == "renewal_due":
        body, rationale = _compose_renewal(merchant, trigger)
    elif kind == "category_seasonal":
        body, rationale = _compose_category_seasonal(category, merchant, trigger)
    elif kind == "active_planning_intent":
        owner = owner_name(merchant)
        biz = merchant_name(merchant)
        loc = merchant.get("identity", {}).get("locality", "your area")
        body = (
            f"{owner}, I saw your note about starting a corporate plan for {biz} in {loc}. "
            "I can draft a tiered pricing proposal and identified 3 office clusters in your delivery radius. "
            "Reply YES and I will send the first version for you to edit."
        )
        rationale = "Intent transition trigger routed to action mode with immediate next artifact."
    else:
        body, rationale = _compose_generic(category, merchant, trigger)

    # --- Step 2: Hidden Data Injection (Master Plan Fix) ---
    # We gather the raw payload and relationship data as hidden context for the LLM.
    trigger_data = format_data_block(trigger.get("payload", {}), "TriggerPayload")
    customer_data = ""
    if customer:
        customer_data = format_data_block(customer.get("relationship", {}), "CustomerRelationship")
        customer_data += format_data_block(customer.get("identity", {}), "CustomerIdentity")

    hidden_facts = f"--- HIDDEN CONTEXT ---\n{trigger_data}{customer_data}"

    # Grammar repair
    body = body.replace("views is", "views are").replace("calls is", "calls are").replace("reviews is", "reviews are")
    
    # Taboo filter
    taboos = category.get("voice", {}).get("vocab_taboo", [])
    for taboo in taboos:
        if taboo.lower() in body.lower():
            body = re.sub(re.escape(taboo), "outcome", body, flags=re.IGNORECASE)
            rationale += f" [Taboo '{taboo}' replaced]"

    pref = language_pref(customer, merchant)
    if "hi" in pref and "reply" in body.lower():
        body = body.replace("Reply", "Reply / jawab")

    # --- Step 3: Hybrid LLM Drafting ---
    if os.getenv("RULE_BASED_ONLY") != "true" and send_as in {"vera", "merchant_on_behalf"}:
        # Extract rich category expertise
        cat_insights = []
        for item in category.get("digest", []):
            cat_insights.append(f"- {item.get('title')}: {item.get('summary')} (Insight: {item.get('actionable')})")
        for beat in category.get("seasonal_beats", []):
            cat_insights.append(f"- Season {beat.get('month_range')}: {beat.get('note')}")
        
        category_context_str = "\n".join(cat_insights)

        try:
            llm_body, llm_rationale = await asyncio.to_thread(
                llm_composer.draft_message,
                category=category,
                merchant=merchant,
                trigger=trigger,
                rule_body=body,
                language_pref=pref,
                scope=scope,
                category_context=category_context_str,
                hidden_facts=hidden_facts,
                customer=customer
            )
            body = llm_body
            rationale += f" | {llm_rationale}"
        except Exception as e:
            rationale += f" | LLM Exception: {str(e)}"

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
        "team_name": "Team Vedant",
        "team_members": ["Vedant"],
        "model": "Vera_Hybrid_LLM_v1",
        "approach": "hybrid rule-based safety + deepseek-chat drafting with dynamic context injection",
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
    tick_guard_deadline_s = 25.0
    seen_pairs: set[tuple[str, str]] = set()

    # Pre-filter triggers
    valid_tasks = []
    for trg_id in body.available_triggers:
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

        valid_tasks.append((category, merchant, trigger, customer, trg_id, s_key, n_key, ns))

    # Process in parallel with gather
    async def _process_one(task):
        category, merchant, trigger, customer, trg_id, s_key, n_key, ns = task
        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")
        
        composed = await compose(category, merchant, trigger, customer)
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
        return action, conv, s_key, n_key, ns

    if valid_tasks:
        # Cap at 20 as per brief §5
        valid_tasks = valid_tasks[:20]
        results = await asyncio.gather(*[_process_one(t) for t in valid_tasks], return_exceptions=True)
        
        for res in results:
            if isinstance(res, Exception):
                continue
            if (time.monotonic() - tick_started) > tick_guard_deadline_s:
                break
                
            action, conv, s_key, n_key, ns = res
            pair = (action["merchant_id"], action["conversation_id"])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            err = _validate_action_payload(action, conv)
            if err:
                continue

            conv.message_hashes.add(text_hash(action["body"]))
            conv.history.append({"role": "vera", "msg": action["body"]})
            conversations[action["conversation_id"]] = conv
            suppression_sent[s_key] = utc_now_iso()
            ns.sent_count += 1
            nudge_state[n_key] = ns
            actions.append(action)

    return {"actions": actions}


def detect_message_language(msg: str) -> Optional[str]:
    """Detects if a message is primarily in a specific Indic language based on script or common keywords."""
    m = msg.lower()
    # Script-based detection (simple regex for common Indic ranges)
    if re.search(r"[\u0900-\u097F]", msg): return "hi-en mix" # Devanagari (Hindi/Marathi)
    if re.search(r"[\u0B80-\u0BFF]", msg): return "ta-en mix" # Tamil
    if re.search(r"[\u0C00-\u0C7F]", msg): return "te-en mix" # Telugu
    if re.search(r"[\u0C80-\u0CFF]", msg): return "kn-en mix" # Kannada
    if re.search(r"[\u0980-\u09FF]", msg): return "bn-en mix" # Bengali
    if re.search(r"[\u0A00-\u0A7F]", msg): return "pa-en mix" # Punjabi
    if re.search(r"[\u0A80-\u0AFF]", msg): return "gu-en mix" # Gujarati
    if re.search(r"[\u0D00-\u0D7F]", msg): return "ml-en mix" # Malayalam
    if re.search(r"[\u0B00-\u0B7F]", msg): return "or-en mix" # Odia
    if re.search(r"[\u0980-\u09FF]", msg): return "as-en mix" # Assamese (same script as Bengali, but distinct code)
    
    # Romanized keyword detection for high-frequency switch signals
    hindi_roman = ["karo", "hai", "nahi", "kya", "aap", "bol", "raha", "karun", "bhejo", "mat"]
    if any(f" {w} " in f" {m} " for w in hindi_roman): return "hi-en mix"
    
    return None


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

    # Store user message in history
    conv.history.append({"role": "user", "msg": msg})

    # --- Dual-Layer Intelligence ---
    llm_analysis = llm_composer.analyze_reply_context(conv.history, msg)
    llm_pref = None
    llm_intent = None
    is_auto_reply = looks_auto_reply(msg)
    
    signal = detect_hostility_signal(msg)

    if llm_analysis:
        h_level = llm_analysis.get("hostility", "none")
        llm_intent = llm_analysis.get("intent", "qualification")
        
        if h_level == "hard_stop": signal = HostilitySignal("hard_stop", 1.0)
        elif h_level == "high_hostile": signal = HostilitySignal("high_hostile", 1.0)
        elif h_level == "medium_frustration": signal = HostilitySignal("medium_frustration", 1.0)
        elif h_level == "auto_reply": is_auto_reply = True
        
        llm_pref_raw = llm_analysis.get("language")
        if llm_pref_raw and llm_pref_raw != "en":
            llm_pref = f"{llm_pref_raw}-en mix"
        elif llm_pref_raw == "en":
            llm_pref = "english"

    if signal.kind == "hard_stop":
        _close_conversation(conv)
        return {"action": "end", "rationale": "Detected explicit opt-out/not-interested signal; closing immediately."}

    if signal.kind == "high_hostile":
        conv.hostility_score += 2
        conv.last_hostility_kind = signal.kind
        _close_conversation(conv)
        return {"action": "end", "rationale": "Detected high-confidence hostile signal; closing gracefully to avoid escalation."}

    policy_intent = "QUALIFICATION"
    out_body = ""
    out_cta = "open_ended"
    out_rationale = ""

    if signal.kind == "medium_frustration":
        conv.hostility_score += 1
        conv.last_hostility_kind = signal.kind
        if conv.hostility_score >= 2:
            _close_conversation(conv)
            return {"action": "end", "rationale": "Repeated frustration signals detected; ending conversation respectfully."}
        policy_intent = "DE_ESCALATION"
        out_body = "Sorry for the disturbance. I’ll keep this brief. If you prefer no further messages, reply STOP."
        out_cta = "binary_yes_no"
        out_rationale = "Medium-confidence frustration detected; sent one de-escalation step."

    elif is_auto_reply:
        ar_key = (body.merchant_id or "", body.customer_id or "")
        global_auto_reply_count[ar_key] = global_auto_reply_count.get(ar_key, 0) + 1
        current_ar_count = global_auto_reply_count[ar_key]
        
        if current_ar_count == 1:
            first_action = _auto_reply_first_action(conv)
            if first_action == "wait":
                wait_seconds = env_int("AUTO_REPLY_WAIT_SECONDS", 14400)
                return {"action": "wait", "wait_seconds": max(300, wait_seconds), "rationale": "Detected canned auto-reply; policy selected immediate backoff for first occurrence."}
            policy_intent = "AUTO_REPLY_NUDGE"
            out_body = "Looks like an auto-reply. When the owner is available, reply YES and I will continue from here."
            out_cta = "binary_yes_no"
            out_rationale = "Detected canned auto-reply; policy selected one owner-directed nudge on first occurrence."
        elif current_ar_count >= 3:
            _close_conversation(conv)
            return {"action": "end", "rationale": "Auto-reply repeated 3 times with no engagement; ending conversation."}
        elif current_ar_count == 2:
            _close_conversation(conv)
            policy_intent = "GRACEFUL_EXIT"
            out_body = "Understood. I will leave a note and connect with the owner later. Have a good day."
            out_cta = "open_ended"
            out_rationale = "Second consecutive auto-reply detected; closing conversation gracefully with a final send."

    else:
        # Any non-auto inbound is meaningful engagement
        n_key = _conv_nudge_key(conv)
        ns = nudge_state.get(n_key, NudgeState())
        ns.engaged = True
        nudge_state[n_key] = ns

        if llm_intent == "action_commitment" or (not llm_analysis and looks_action_intent(msg)):
            policy_intent = "ACTION_COMMITMENT"
            artifact_reply = _artifact_reply_for_action_intent(conv, msg)
            if artifact_reply:
                out_body = artifact_reply["body"]
                out_cta = artifact_reply["cta"]
                out_rationale = "Detected explicit commitment plus concrete artifact request; returned requested artifacts in the same turn."
            else:
                out_body = "Great. Moving to action now: I can draft the exact next message and checklist in one go. Reply CONFIRM to proceed."
                out_cta = "binary_confirm_cancel"
                out_rationale = "Detected explicit commitment; switched from qualification to action mode."

        elif llm_intent == "off_topic" or (not llm_analysis and looks_off_topic(msg)):
            policy_intent = "OFF_TOPIC_REDIRECT"
            out_body = "I should leave GST/CA work to your accountant. On this thread, I can help with the current business trigger. Want me to continue?"
            out_cta = "open_ended"
            out_rationale = "Polite out-of-scope handling with redirect to active context."

        else:
            policy_intent = "QUALIFICATION"
            out_body = "Understood. I can take this forward and send a concise next-step draft now. Want that?"
            out_cta = "open_ended"
            out_rationale = "Acknowledged response and advanced toward a concrete next step."

    # --- Step 4: Hybrid LLM Reply Drafting ---
    if os.getenv("RULE_BASED_ONLY") != "true" and out_body:
        # Get context records for the LLM
        merchant = contexts.get(("merchant", conv.merchant_id), {}).get("payload", {})
        category = contexts.get(("category", merchant.get("category_slug", "")), {}).get("payload", {})
        trigger = contexts.get(("trigger", conv.trigger_id), {}).get("payload", {}) if conv.trigger_id else None
        customer = contexts.get(("customer", conv.customer_id), {}).get("payload", {}) if conv.customer_id else None
        
        # Per-turn language switch detection
        detected_pref = llm_pref or detect_message_language(msg)
        pref = detected_pref if detected_pref else language_pref(customer, merchant)

        llm_reply, llm_rationale = llm_composer.respond(
            category=category,
            merchant=merchant,
            trigger=trigger,
            customer=customer,
            history=conv.history[:-1], # pass history excluding latest
            latest_message=msg,
            policy_intent=policy_intent,
            rule_body=out_body,
            language_pref=pref
        )
        out_body = llm_reply
        out_rationale += f" | {llm_rationale}"

    # Final safe check and history update
    safe = safe_body_or_none(out_body) or out_body
    conv.history.append({"role": "vera", "msg": safe})
    conv.message_hashes.add(text_hash(safe))

    return {"action": "send", "body": safe, "cta": out_cta, "rationale": out_rationale}


@app.post("/v1/teardown", response_model=TeardownResponse)
async def teardown():
    contexts.clear()
    conversations.clear()
    suppression_sent.clear()
    nudge_state.clear()
    global_auto_reply_count.clear()
    global conv_counter
    conv_counter = 0
    return {"ok": True, "wiped_at": utc_now_iso()}
