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
        rule_nums = (
            Validator.extract_numbers(rule_body) | 
            Validator.extract_numbers(category_context) |
            Validator.extract_numbers(hidden_facts)
        )
        
        for num in llm_nums:
            # Allow common small numbers or years like 2026
            if num not in rule_nums and float(num) > 5 and num != "2026":
                return False, f"Hallucinated number detected: {num}"

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

STRICT PSYCHOLOGICAL RULES:
1. NO SHAME / NO GUILT: If the message mentions a lapse or long absence, use a 'no judgment' framing. NEVER sound needy or pushy.
2. ULTRA-LOW FRICTION: If the factual message contains choices or slots, YOU MUST format the CTA as: "Reply 1 for [Choice A], 2 for [Choice B], or tell us a time that works."
3. MOLECULE PRECISION: For pharmacy refills, always explicitly list the medicine names.

STRICT OPERATIONAL RULES:
1. MANDATORY CODE-MIXING: Start with a greeting in {mix_lang}. You MUST mix {mix_lang} and English throughout the message.
2. VERIFIABLE SPECIFICITY & CITATIONS: Weave the provided facts and dates into the message naturally. Cite sources like 'according to our records'.
3. PUNCHY & BRIEF: Keep the total length under 150 words. No filler.
4. No 'Vera' or 'magicpin'. No URLs.
5. ANTI-HALLUCINATION: Use explicit facts only. Do not claim loyalty status unless context supports it.
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

STRICT OPERATOR RULES:
1. OPERATOR LEXICON: Use industry terms ('covers', 'AOV', 'CTR', 'retention'). Speak operator-to-operator.
2. CONTRARIAN INSIGHT: If the factual message asks for a plan, use the CATEGORY EXPERTISE to provide a brilliant, expert strategic recommendation.
3. CITATIONS & CONTEXT FUSION: Cite sources naturally (e.g. 'according to magicpin performance data', 'based on industry benchmarks'). You MUST tie industry insights directly to the merchant's current performance data.
4. MANDATORY CODE-MIXING: Start with a greeting in {mix_lang}. You MUST mix {mix_lang} and English throughout.
5. AGGRESSIVE BREVITY: Start immediately with the insight. No intro filler. TOTAL LENGTH: Maximum 180 words.
6. NO ASSUMPTIONS: Do NOT assume the merchant has specific revenue splits, inventory, or equipment unless explicitly stated. Stick to general industry trends if context is missing.

STRICT OPERATIONAL RULES:
1. Speak as Vera. Use ONLY provided facts. DO NOT invent prices or percentages.
2. No URLs. Use Roman script for {mix_lang}.
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
            return rule_body, f"LLM Rejected: {reason} (Fallback to Rules)"
            
    except Exception as e:
        return rule_body, f"LLM Error: {str(e)} (Fallback to Rules)"
