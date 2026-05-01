import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib import request as urlrequest, error as urlerror

# API Configuration
LLM_API_KEY = "sk-aa7ab0f6091e4f238bc5fc1d6f4ed313"
LLM_MODEL = "deepseek-chat"
TIMEOUT_LLM = 10  # Strict timeout for bot responsiveness

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
                "temperature": 0.1,  # Low temperature for stability
                "max_tokens": 800  # Increased for multilingual richness
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}", 
                "Content-Type": "application/json"
            }
        )
        resp = urlrequest.urlopen(req, timeout=TIMEOUT_LLM)
        data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()

class Validator:
    @staticmethod
    def extract_numbers(text: str) -> set:
        # Extracts numbers like 10, 15.5, 40%, etc.
        return set(re.findall(r'\d+(?:\.\d+)?', text))

    @staticmethod
    def validate(llm_body: str, rule_body: str, category_context: str, hidden_facts: str, taboos: List[str]) -> Tuple[bool, str]:
        # 1. URL Check
        if re.search(r'https?://\S+|www\.\S+', llm_body):
            return False, "Contains URL"

        # 2. Taboo Check
        for taboo in taboos:
            if taboo.lower() in llm_body.lower():
                return False, f"Contains taboo word: {taboo}"

        # 3. Numeric Provenance (Hard Gate)
        # Every number in the LLM output MUST exist in the rule-based output, category expertise, OR hidden facts
        llm_nums = Validator.extract_numbers(llm_body)
        
        # Convert all available facts to a set of floats for loose but safe matching
        fact_texts = [rule_body, category_context, hidden_facts]
        allowed_floats = set()
        for text in fact_texts:
            for n in Validator.extract_numbers(text):
                try:
                    allowed_floats.add(float(n))
                except ValueError:
                    continue
        
        for num_str in llm_nums:
            try:
                num_val = float(num_str)
                # Allow common small numbers, years like 2026, or any number explicitly in facts
                if num_val <= 5 or num_val == 2026 or num_val in allowed_floats:
                    continue
                return False, f"Hallucinated number detected: {num_str}"
            except ValueError:
                continue

        return True, "Valid"

def draft_message(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    rule_body: str,
    language_pref: str = "en",
    scope: str = "merchant",
    category_context: str = "",
    hidden_facts: str = ""
) -> Tuple[str, str]:
    provider = DeepSeekProvider(LLM_API_KEY, LLM_MODEL)
    
    cat_name = category.get("name", "Business")
    biz_name = merchant.get("identity", {}).get("name", "your business")
    owner = merchant.get("identity", {}).get("owner_name", "there")
    voice = category.get("voice", {})
    tone = voice.get("tone", "professional and helpful")
    taboos = voice.get("vocab_taboo", [])
    
    # Extract mixing language if present
    mix_lang = "Hindi" # Default fallback
    if "-en" in language_pref:
        lang_code = language_pref.split("-")[0]
        lang_map = {"hi": "Hindi", "te": "Telugu", "ta": "Tamil", "kn": "Kannada", "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi", "ml": "Malayalam"}
        mix_lang = lang_map.get(lang_code, lang_code.upper())

    if scope == "customer":
        is_coaching = cat_name.lower() in ["gym", "fitness", "salon", "yoga", "dentist"]
        tone_instruction = f"adopting a {tone} tone"
        if is_coaching:
            tone_instruction = f"acting as a supportive and professional {cat_name} partner, using an encouraging and motivational {tone} tone"

        system_prompt = f"""You are the AI representative of '{biz_name}', a {cat_name}. 
Your goal is to rewrite a factual reminder into a highly personalized and engaging message for a customer.

CONTEXT:
- Tone: {tone_instruction}
- Language: {language_pref} (CRITICAL: You MUST write in a natural mix of {mix_lang} and English using Roman script).
- CATEGORY INSIGHTS: {category_context}

STRICT MESSAGE STRUCTURE:
Your message must follow this exact 3-part flow:
1. [Grounded Insight]: Establish why you are messaging using a concrete fact.
2. [Contextual Benefit]: Explain why this matters to the customer.
3. [Call to Action]: One specific question or directive.

STRICT PSYCHOLOGICAL RULES:
1. NO SHAME / NO GUILT: If the message mentions a lapse or long absence, use a 'no judgment' framing. NEVER sound needy or pushy.
2. ULTRA-LOW FRICTION: If the factual message contains choices or slots, YOU MUST format the CTA as: "Reply 1 for [Choice A], 2 for [Choice B], or tell us a time that works."
3. MOLECULE PRECISION: For pharmacy refills, always explicitly list the medicine names.

STRICT OPERATIONAL RULES:
1. TERMINAL HOOK RULE: The Call to Action (CTA) MUST be the absolute final sentence of your message. NEVER add sign-offs, signatures, or pleasantries (like 'Regards' or 'Have a great day') after the CTA.
2. SINGLE OBJECTIVE RULE: Focus exclusively on one goal. Do not ask multiple independent questions or suggest unrelated actions.
3. MANDATORY CODE-MIXING: Start with a greeting in {mix_lang}. You MUST mix {mix_lang} and English throughout the message.
4. VERIFIABLE SPECIFICITY & CITATIONS: Weave the provided facts and dates into the message naturally. Cite sources like 'according to our records'.
5. PUNCHY & BRIEF: Keep the total length under 150 words. No filler.
6. No 'Vera' or 'magicpin'. No URLs.
7. ANTI-HALLUCINATION: Use explicit facts only. Do not extrapolate visit counts.
"""
    else:
        # Role: Vera (AI Assistant) talking to Merchant
        system_prompt = f"""You are Vera, an expert AI merchant assistant for magicpin.
Your goal is to rewrite a factual update into an engaging, coaching-style message for the business owner, {owner}.

CONTEXT:
- Category: {cat_name}
- Tone: {tone}
- Merchant: {biz_name}
- Language: {language_pref} (CRITICAL: You MUST write in a professional mix of {mix_lang} and English using Roman script).
- CATEGORY EXPERTISE: {category_context}

STRICT MESSAGE STRUCTURE:
Your message must follow this exact 3-part flow:
1. [Grounded Insight]: Start immediately with the insight or performance signal.
2. [Expert Coaching]: Provide a strategic recommendation or "Why this matters."
3. [Terminal CTA]: One specific question to advance the action.

STRICT OPERATOR RULES:
1. TERMINAL HOOK RULE: The Call to Action (CTA) MUST be the absolute final sentence of your message. NEVER add sign-offs, pleasantries, or conclusions after the CTA.
2. SINGLE OBJECTIVE RULE: Maintain a single focus. Do not ask more than one question or propose more than one independent action.
3. OPERATOR LEXICON: Use industry terms ('covers', 'AOV', 'CTR', 'retention'). Speak operator-to-operator.
4. CONTRARIAN INSIGHT: If the factual message asks for a plan, use the CATEGORY EXPERTISE to provide a brilliant, expert strategic recommendation.
5. CITATIONS & CONTEXT FUSION: Cite sources naturally. You MUST tie industry insights directly to the merchant's current performance data.
6. MANDATORY CODE-MIXING: Start with a greeting in {mix_lang}. You MUST mix {mix_lang} and English throughout.
7. AGGRESSIVE BREVITY: Maximum 180 words. Start immediately with the insight.
8. ANTI-HALLUCINATION: Use ONLY provided facts. DO NOT invent prices or percentages.
"""

    user_prompt = f"""FACTUAL MESSAGE:
"{rule_body}"

{hidden_facts}

Rewrite this message to be more engaging while strictly following the rules above. 
Use the data in the --- HIDDEN CONTEXT --- to make the message highly specific and verifiable, but DO NOT include the raw "HIDDEN CONTEXT" text or labels in your final output.
OUTPUT ONLY THE MESSAGE BODY. NO INTRO, NO OUTRO, NO QUOTES."""

    try:
        start_time = time.time()
        llm_output = provider.complete(user_prompt, system=system_prompt)
        latency = time.time() - start_time
        
        # Cleanup
        llm_output = llm_output.strip('"').strip("'").strip()
        
        is_valid, reason = Validator.validate(llm_output, rule_body, category_context, hidden_facts, taboos)
        
        if is_valid:
            return llm_output, f"LLM Drafted ({latency:.2f}s)"
        else:
            # --- Self-Correction Loop (1 Retry) ---
            repair_prompt = (
                f"{user_prompt}\n\n"
                f"--- YOUR PREVIOUS DRAFT ---\n{llm_output}\n\n"
                f"--- VALIDATOR REJECTION REASON ---\n"
                f"{reason}\n\n"
                f"Rewrite the message to fix this error. If it was a hallucinated number, remove or correct it. "
                f"If it was a taboo word, change it. Maintain the tone and code-mixing.\n"
                f"OUTPUT ONLY THE REPAIRED MESSAGE BODY. NO INTRO, NO OUTRO, NO QUOTES."
            )
            
            retry_start = time.time()
            repaired_output = provider.complete(repair_prompt, system=system_prompt)
            retry_latency = time.time() - retry_start
            
            repaired_output = repaired_output.strip('"').strip("'").strip()
            is_valid_repaired, new_reason = Validator.validate(repaired_output, rule_body, category_context, hidden_facts, taboos)
            
            if is_valid_repaired:
                return repaired_output, f"LLM Repaired ({latency + retry_latency:.2f}s) [Fixed: {reason}]"
            else:
                return rule_body, f"LLM Rejected Twice: {new_reason} (Fallback to Rules)"
            
    except Exception as e:
        return rule_body, f"LLM Error: {str(e)} (Fallback to Rules)"


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
    
    cat_name = category.get("name", "Business")
    biz_name = merchant.get("identity", {}).get("name", "your business")
    owner = merchant.get("identity", {}).get("owner_name", "there")
    voice = category.get("voice", {})
    tone = voice.get("tone", "professional and helpful")
    taboos = voice.get("vocab_taboo", [])
    
    # Extract mixing language
    mix_lang = "Hindi"
    if "-en" in language_pref:
        lang_code = language_pref.split("-")[0]
        lang_map = {"hi": "Hindi", "te": "Telugu", "ta": "Tamil", "kn": "Kannada", "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi", "ml": "Malayalam"}
        mix_lang = lang_map.get(lang_code, lang_code.upper())

    # Build history string
    history_str = "\n".join([f"{h['role'].upper()}: {h['msg']}" for h in history[-5:]])

    system_prompt = f"""You are Vera, an expert AI industry peer for '{biz_name}' ({cat_name}).
Your goal is to continue a conversation with the merchant/customer naturally and professionally.

CONTEXT:
- Category: {cat_name}
- Tone: {tone}
- Language: {language_pref} (You MUST write in a natural mix of {mix_lang} and English).
- POLICY INTENT: {policy_intent}

STRICT CONVERSATIONAL RULES:
1. PERSISTENCE: Maintain the expert-peer persona established in previous turns.
2. NO REPETITION: Do NOT repeat facts or questions already discussed in the HISTORY.
3. INTENT ALIGNMENT: 
   - If POLICY INTENT is 'AUTO_REPLY_NUDGE', be polite and acknowledge you'll wait for the owner.
   - If POLICY INTENT is 'ACTION_COMMITMENT', generate the final draft/plan/artifact immediately.
   - If POLICY INTENT is 'DE_ESCALATION', be extremely respectful and offer a clear way to stop.
4. BREVITY: Keep replies under 60 words. No filler.
5. MANDATORY CODE-MIXING: Start with a greeting if it's the start of the message.
"""

    user_prompt = f"""CONVERSATION HISTORY:
{history_str}

USER LATEST MESSAGE:
"{latest_message}"

FACTUAL BASELINE (from rules):
"{rule_body}"

Write the next response in the conversation while strictly following the rules above."""

    try:
        start_time = time.time()
        llm_output = provider.complete(user_prompt, system=system_prompt)
        latency = time.time() - start_time
        
        llm_output = llm_output.strip('"').strip("'").strip()
        
        # Loose validation for replies (safety focus)
        if re.search(r'https?://\S+|www\.\S+', llm_output):
             return rule_body, f"LLM Reply Rejected: Contains URL"
        
        for taboo in taboos:
            if taboo.lower() in llm_output.lower():
                return rule_body, f"LLM Reply Rejected: Taboo word"

        return llm_output, f"LLM Responded ({latency:.2f}s)"
            
    except Exception as e:
        return rule_body, f"LLM Reply Error: {str(e)}"
