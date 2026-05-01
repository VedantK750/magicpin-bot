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
                "max_tokens": 500
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
    def validate(llm_body: str, rule_body: str, taboos: List[str]) -> Tuple[bool, str]:
        # 1. URL Check
        if re.search(r'https?://\S+|www\.\S+', llm_body):
            return False, "Contains URL"

        # 2. Taboo Check
        for taboo in taboos:
            if taboo.lower() in llm_body.lower():
                return False, f"Contains taboo word: {taboo}"

        # 3. Numeric Provenance (Hard Gate)
        # Every number in the LLM output MUST exist in the rule-based output
        llm_nums = Validator.extract_numbers(llm_body)
        rule_nums = Validator.extract_numbers(rule_body)
        
        # We allow common small numbers like 1, 2, 3 if they are used for steps
        # but anything else must be verified.
        for num in llm_nums:
            if num not in rule_nums and float(num) > 5:
                return False, f"Hallucinated number detected: {num}"

        return True, "Valid"

def draft_message(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    rule_body: str,
    language_pref: str = "en",
    scope: str = "merchant"
) -> Tuple[str, str]:
    provider = DeepSeekProvider(LLM_API_KEY, LLM_MODEL)
    
    cat_name = category.get("name", "Business")
    biz_name = merchant.get("identity", {}).get("name", "your business")
    owner = merchant.get("identity", {}).get("owner_name", "there")
    voice = category.get("voice", {})
    tone = voice.get("tone", "professional and helpful")
    taboos = voice.get("vocab_taboo", [])
    
    # Extract mixing language if present (e.g., 'te' from 'te-en mix')
    mix_lang = "Hindi" # Default fallback
    if "-en" in language_pref:
        lang_code = language_pref.split("-")[0]
        # Common map for better prompting
        lang_map = {"hi": "Hindi", "te": "Telugu", "ta": "Tamil", "kn": "Kannada", "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi", "ml": "Malayalam"}
        mix_lang = lang_map.get(lang_code, lang_code.upper())

    if scope == "customer":
        # Role: Business talking to Customer
        # Detect if it's a motivational category to adjust tone
        is_coaching = cat_name.lower() in ["gym", "fitness", "salon", "yoga", "dentist"]
        tone_instruction = f"adopting a {tone} tone"
        if is_coaching:
            tone_instruction = f"acting as a supportive and professional {cat_name} partner, using an encouraging and motivational {tone} tone"

        system_prompt = f"""You are the AI representative of '{biz_name}', a {cat_name}. 
Your goal is to rewrite a factual reminder into a highly personalized and engaging message for a customer.

CONTEXT:
- Tone: {tone_instruction}
- Language Preference: {language_pref} (If code-mixing requested, use a natural mix of {mix_lang} and English)

STRICT RULES:
1. Speak as the business ('We', 'Our', or '{biz_name}'). Do NOT mention 'Vera' or 'magicpin'.
2. Use the facts labeled '[Context: ...]' in the factual message to make the message highly specific and verifiable.
3. If a goal (e.g., 'weight loss') or session history is mentioned, be motivational about their progress.
4. If a child name is mentioned, address the parent warmly about the child's next step.
5. Use ONLY the provided facts. Do NOT invent prices or specific dates not in the factual message.
6. Keep it concise. No URLs.
7. Avoid these taboo words: {', '.join(taboos)}
8. If code-mixing, use Roman script for {mix_lang}.
9. ANTI-HALLUCINATION: Use explicit facts only. Do not claim loyalty status unless context supports it.
"""
    else:
        system_prompt = f"""You are Vera, an expert AI merchant assistant for magicpin.
Your goal is to rewrite a factual update into an engaging, coaching-style message for the business owner, {owner}.

CONTEXT:
- Category: {cat_name}
- Tone: {tone}
- Merchant: {biz_name}
- Language Preference: {language_pref} (If code-mixing requested, use a professional mix of {mix_lang} and English)

STRICT RULES:
1. Speak as Vera (the assistant).
2. Use ONLY the facts provided in the factual message. 
3. DO NOT invent any new numbers, percentages, or data.
4. DO NOT make claims about the merchant's business status (e.g., "you are compliant", "you are state-of-the-art") unless the factual message says so.
5. If the factual message is an invitation or alert, stay in that mode.
6. Do NOT include any URLs.
7. Avoid these taboo words: {', '.join(taboos)}
8. If code-mixing, use Roman script for {mix_lang}.
"""

    user_prompt = f"""FACTUAL MESSAGE:
"{rule_body}"

Rewrite this message to be more engaging while strictly following the rules above. 
OUTPUT ONLY THE MESSAGE BODY. NO INTRO, NO OUTRO, NO QUOTES."""

    try:
        start_time = time.time()
        llm_output = provider.complete(user_prompt, system=system_prompt)
        latency = time.time() - start_time
        
        # Aggressive cleanup of LLM filler
        llm_output = llm_output.strip('"').strip("'").strip()
        if "---" in llm_output:
            llm_output = llm_output.split("---")[-1].strip()
        if ":" in llm_output[:30] and ("rewrite" in llm_output[:50].lower() or "message" in llm_output[:50].lower()):
            llm_output = llm_output.split(":", 1)[1].strip()
        
        is_valid, reason = Validator.validate(llm_output, rule_body, taboos)
        
        if is_valid:
            return llm_output, f"LLM Drafted ({latency:.2f}s)"
        else:
            return rule_body, f"LLM Rejected: {reason} (Fallback to Rules)"
            
    except Exception as e:
        return rule_body, f"LLM Error: {str(e)} (Fallback to Rules)"
