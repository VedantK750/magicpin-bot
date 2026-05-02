import json
import re
import time
import os
import concurrent.futures
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Literal
from urllib import request as urlrequest, error as urlerror

# API Configuration
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_MODEL = "deepseek-chat"
TIMEOUT_LLM = 25 

@dataclass
class ComposerState:
    # Raw Context
    category: Dict[str, Any]
    merchant: Dict[str, Any]
    trigger: Optional[Dict[str, Any]]
    customer: Optional[Dict[str, Any]]
    
    # Derived Context
    language_pref: str
    scope: Literal["merchant", "customer"]
    history: List[Dict[str, str]] = field(default_factory=list)
    latest_message: str = ""
    policy_intent: str = ""
    rule_body: str = ""
    
    # Agent Outputs
    dossier: str = ""
    strategy: str = ""
    draft: str = ""
    critique: str = ""
    validation_passed: bool = False
    
    # Metadata
    total_latency: float = 0.0
    iterations: int = 0

class DeepSeekProvider:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def complete(self, prompt: str, system: str = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        req = urlrequest.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=json.dumps({
                "model": self.model, 
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 1000
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}", 
                "Content-Type": "application/json"
            }
        )
        resp = urlrequest.urlopen(req, timeout=TIMEOUT_LLM)
        data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()

def _get_mix_lang(pref: str) -> str:
    pref = pref.lower()
    lang_map = {
        "hi": "Hindi", "te": "Telugu", "ta": "Tamil", "kn": "Kannada", 
        "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi", 
        "ml": "Malayalam", "or": "Odia", "as": "Assamese"
    }
    for code, name in lang_map.items():
        if code in pref: return name
    return "English"

def draft_message(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    rule_body: str,
    language_pref: str = "en",
    scope: Literal["merchant", "customer"] = "merchant",
    category_context: str = "",
    hidden_facts: str = "",
    customer: Optional[Dict[str, Any]] = None
) -> Tuple[str, str]:
    provider = DeepSeekProvider(LLM_API_KEY, LLM_MODEL)
    
    cat_slug = category.get("slug", "business")
    biz_name = merchant.get("identity", {}).get("name", "your business")
    owner = merchant.get("identity", {}).get("owner_name") or merchant.get("identity", {}).get("owner_first_name", "there")
    if cat_slug == "dentists" and not str(owner).startswith("Dr."):
        owner = f"Dr. {owner}"
    
    mix_lang = _get_mix_lang(language_pref)
    lang_instruction = f"MANDATORY CODE-MIXING: Use a natural mix of {mix_lang} and English in Roman script."
    if mix_lang == "English":
        lang_instruction = "LANGUAGE: Use clear, professional English."
    
    # Context summary for the LLM to prevent hallucination
    active_offers = [f"{o.get('title')} (@ {o.get('price')})" for o in merchant.get("offers", []) if o.get("status") == "active"]
    perf = merchant.get("performance", {})
    metrics = f"views={perf.get('views', 'N/A')}, calls={perf.get('calls', 'N/A')}, ctr={perf.get('ctr', 'N/A')}"
    
    # Extract specific insight source if provided in trigger
    insight_context = ""
    top_item_id = trigger.get("payload", {}).get("top_item_id")
    if top_item_id:
        for item in category.get("digest", []):
            if item.get("id") == top_item_id:
                src = item.get('source', 'magicpin analytics')
                insight_context = f"\n- Relevant Insight: {item.get('title')} [Source: {src}]. {item.get('summary')}"
                break
    
    customer_cta_rule = ""
    if scope == "customer":
        customer_cta_rule = '\n11. ULTRA-LOW FRICTION CTA: Because this is a customer-facing message, if proposing slots, options, or products, YOU MUST format the CTA as multi-choice: "Reply 1 for [Choice A], 2 for [Choice B], or let us know your preference."'
    
    system_prompt = f"""You are Vera, an expert AI partner for {cat_slug} on magicpin.
Your task is to write a single WhatsApp message.

STRICT CONSTRAINTS (VIOLATION = FAILURE):
1. NO PREAMBLE: Start the message immediately. Never say "Here is your message" or "Insight:".
2. NO BOLD HEADERS: Do not use **Insight** or **Action**. Use plain text.
3. TERMINAL HOOK RULE: The Call to Action (CTA) must be the ABSOLUTE FINAL SENTENCE. No sign-offs like 'Regards' or 'Vera'.
4. NO FABRICATION: Use ONLY facts provided. Cite the exact [Source] if provided in Relevant Insight.
5. MANDATORY OFFER ANCHORING: If Active Offers exist, you MUST connect the insight/problem to the offer as the solution.
6. {lang_instruction}
7. SINGLE OBJECTIVE: Ask exactly ONE question or give one clear directive at the end.
8. CATEGORY VOICE & STRATEGY: 
   - Dentists/Pharmacies: Peer-clinical, precise. NEVER suggest "Loyalty", "Marketing", or "Discounts". Frame actions as "Patient Care", "Clinical Standards", or "Health Checkups".
   - Salons/Gyms: Warm, coaching, practical.
   - Restaurants: Operator-to-operator.
9. MANDATORY DATES: If the Trigger Payload contains a specific date (e.g., opened_date, expires_at), you MUST explicitly state that date in the message.
10. ENGAGEMENT COMPULSION: You MUST inject at least one psychological lever into the message to drive engagement compulsion: Loss Aversion (e.g., "don't let these potential patients drop off"), Social Proof (e.g., referencing high local search volume or views), or Curiosity.{customer_cta_rule}

MESSAGE FLOW:
1. Grounded Insight (use views, calls, or Relevant Insight with its Source).
2. Contextual Benefit & Offer (Why this matters, anchored to their Active Offer to drive loss aversion/conversion).
3. Terminal CTA (One clear question).
"""

    user_prompt = f"""CONTEXT:
- Merchant: {biz_name}
- Owner: {owner}
- Locality: {merchant.get('identity', {}).get('locality')}
- Active Offers: {active_offers}
- Performance: {metrics}
- Trigger: {trigger.get('kind')} (Payload: {json.dumps(trigger.get('payload', {}))}){insight_context}
- Customer: {json.dumps(customer.get('identity', {})) if customer else 'N/A'}

FACTUAL BASELINE: "{rule_body}"

Write the final WhatsApp message body now. NO META-TALK."""

    try:
        start_time = time.time()
        llm_output = provider.complete(user_prompt, system=system_prompt)
        latency = time.time() - start_time
        
        # Post-process cleanup for common LLM artifacts
        llm_output = re.sub(r"^(Vera|Insight|Draft|Message):\s*", "", llm_output, flags=re.I)
        llm_output = llm_output.strip('"').strip("'").strip()
        
        return llm_output, f"Single-Pass Optimized ({latency:.1f}s)"
    except Exception as e:
        return rule_body, f"Error: {str(e)}"

def analyze_reply_context(history: List[Dict[str, str]], latest_message: str) -> Optional[Dict[str, str]]:
    provider = DeepSeekProvider(LLM_API_KEY, LLM_MODEL)
    history_str = "\n".join([f"{h['role'].upper()}: {h['msg']}" for h in history[-3:]])
    
    system_prompt = """You are a conversational analyzer for a WhatsApp business bot.
Analyze the user's latest message in the context of the conversation history.
Output ONLY a raw JSON object with no markdown formatting or explanation.

JSON SCHEMA:
{
  "hostility": "none" | "medium_frustration" | "high_hostile" | "hard_stop" | "auto_reply",
  "intent": "qualification" | "action_commitment" | "off_topic",
  "language": "en" | "hi" | "te" | "ta" | "kn" | "mr" | "bn" | "gu" | "pa" | "ml" | "or" | "as"
}

LANGUAGE DETECTION RULES:
- Detect the exact language used. If it's a mix of English and an Indic language (e.g., Hinglish, Tanglish) in Roman script, or pure Indic script, return the primary Indic language code (hi, te, ta, etc.).
- If it's purely English, return "en".

HOSTILITY RULES:
- "hard_stop": Explicitly asks to stop messaging, unsubscribe, or block.
- "high_hostile": Abusive, angry, or threatening language.
- "medium_frustration": Annoyed but not abusive ("why are you messaging me?").
- "auto_reply": "I am driving", "Out of office".

INTENT RULES:
- "action_commitment": User agrees to proceed, says "yes", "do it", "sure", "ok draft it", "okay".
- "off_topic": User asks about unrelated topics.
- "qualification": Default state. Answering questions or vague interest.
"""

    user_prompt = f"HISTORY:\n{history_str}\n\nUSER: {latest_message}\n\nAnalyze and return JSON:"

    try:
        req = urlrequest.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=json.dumps({
                "model": LLM_MODEL, 
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.0,
                "max_tokens": 100,
                "response_format": {"type": "json_object"}
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}", 
                "Content-Type": "application/json"
            }
        )
        resp = urlrequest.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"].strip()
        
        content = re.sub(r"^```json\s*", "", content, flags=re.I)
        content = re.sub(r"\s*```$", "", content)
        
        return json.loads(content)
    except Exception as e:
        return None

def respond(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Optional[Dict[str, Any]],
    customer: Optional[Dict[str, Any]],
    history: List[Dict[str, str]],
    latest_message: str,
    policy_intent: str,
    rule_body: str,
    language_pref: str = "en"
) -> Tuple[str, str]:
    provider = DeepSeekProvider(LLM_API_KEY, LLM_MODEL)
    mix_lang = _get_mix_lang(language_pref)
    history_str = "\n".join([f"{h['role'].upper()}: {h['msg']}" for h in history[-3:]])
    
    lang_instruction = f"Natural {mix_lang}-English mix."
    if mix_lang == "English":
        lang_instruction = "Use clear, professional English."

    system_prompt = f"""You are Vera, an AI peer for {merchant.get('identity', {}).get('name')}.
{lang_instruction} Policy: {policy_intent}.
Rules: No preamble, No sign-offs, No repetition. Max 60 words."""

    user_prompt = f"HISTORY:\n{history_str}\n\nUSER: {latest_message}\n\nSuggested response: {rule_body}"
    
    try:
        reply = provider.complete(user_prompt, system=system_prompt)
        return reply.strip(), "Optimized Response"
    except Exception as e:
        return rule_body, f"Error: {str(e)}"
