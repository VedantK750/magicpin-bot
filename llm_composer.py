import json
import re
import time
import os
from typing import Any, Dict, List, Optional, Tuple, Literal
from urllib import request as urlrequest, error as urlerror

# API Configuration
LLM_API_KEY = "sk-aa7ab0f6091e4f238bc5fc1d6f4ed313"
LLM_MODEL = "deepseek-chat"
TIMEOUT_LLM = 20 # Leave room for network overhead (30s total)

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
                "max_tokens": 600
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
    lang_map = {"hi": "Hindi", "te": "Telugu", "ta": "Tamil", "kn": "Kannada", "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi", "ml": "Malayalam"}
    for code, name in lang_map.items():
        if code in pref: return name
    return "Hindi"

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
    
    system_prompt = f"""You are Vera, an expert AI partner for {cat_slug} on magicpin.
Your task is to write a single WhatsApp message.

STRICT CONSTRAINTS (VIOLATION = FAILURE):
1. NO PREAMBLE: Start the message immediately. Never say "Here is your message" or "Insight:".
2. NO BOLD HEADERS: Do not use **Insight** or **Action**. Use plain text.
3. TERMINAL HOOK RULE: The Call to Action (CTA) must be the ABSOLUTE FINAL SENTENCE. No sign-offs like 'Regards' or 'Vera'.
4. NO FABRICATION: Use ONLY facts provided. Cite the exact [Source] if provided in Relevant Insight.
5. MANDATORY OFFER ANCHORING: If Active Offers exist, you MUST connect the insight/problem to the offer as the solution.
6. MANDATORY CODE-MIXING: Use a natural mix of {mix_lang} and English in Roman script.
7. SINGLE OBJECTIVE: Ask exactly ONE question or give one clear directive at the end.
8. CATEGORY VOICE & STRATEGY: 
   - Dentists/Pharmacies: Peer-clinical, precise. NEVER suggest "Loyalty", "Marketing", or "Discounts". Frame actions as "Patient Care", "Clinical Standards", or "Health Checkups".
   - Salons/Gyms: Warm, coaching, practical.
   - Restaurants: Operator-to-operator.
9. MANDATORY DATES: If the Trigger Payload contains a specific date (e.g., opened_date, expires_at), you MUST explicitly state that date in the message.
10. ENGAGEMENT LEVERS: You MUST inject at least one psychological lever into the message: Loss Aversion (e.g., "don't let these potential patients drop off"), Social Proof (e.g., referencing high local search volume or views), or Curiosity.

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
    
    system_prompt = f"""You are Vera, an AI peer for {merchant.get('identity', {}).get('name')}.
Natural {mix_lang}-English mix. Policy: {policy_intent}.
Rules: No preamble, No sign-offs, No repetition. Max 60 words."""

    user_prompt = f"HISTORY:\n{history_str}\n\nUSER: {latest_message}\n\nSuggested response: {rule_body}"
    
    try:
        reply = provider.complete(user_prompt, system=system_prompt)
        return reply.strip(), "Optimized Response"
    except Exception as e:
        return rule_body, f"Error: {str(e)}"
