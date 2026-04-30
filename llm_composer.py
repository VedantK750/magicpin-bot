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
    language_pref: str = "en"
) -> Tuple[str, str]:
    provider = DeepSeekProvider(LLM_API_KEY, LLM_MODEL)
    
    cat_name = category.get("name", "Business")
    biz_name = merchant.get("identity", {}).get("name", "your business")
    owner = merchant.get("identity", {}).get("owner_name", "there")
    voice = category.get("voice", {})
    tone = voice.get("tone", "professional and helpful")
    taboos = voice.get("vocab_taboo", [])
    
    system_prompt = f"""You are Vera, an expert AI merchant assistant for magicpin.
Your goal is to rewrite a factual message into a highly engaging, category-specific message.

CONTEXT:
- Category: {cat_name}
- Tone: {tone}
- Merchant: {biz_name} (Owner: {owner})
- Language Preference: {language_pref} (If 'hi-en', use a natural mix of Hindi and English)

STRICT RULES:
1. DO NOT invent any new facts, numbers, dates, or prices. 
2. Use ONLY the facts provided in the factual message.
3. Keep it concise (under 160 characters if possible).
4. Do NOT include any URLs.
5. Avoid these taboo words: {', '.join(taboos)}
6. If the language is 'hi-en', use Roman Hindi (Hinglish) naturally.
"""

    user_prompt = f"""FACTUAL MESSAGE:
"{rule_body}"

Rewrite this message to be more engaging and personalized while strictly following the rules above."""

    try:
        start_time = time.time()
        llm_output = provider.complete(user_prompt, system=system_prompt)
        latency = time.time() - start_time
        
        # Remove quotes if the LLM wrapped the message
        llm_output = llm_output.strip('"').strip("'")
        
        is_valid, reason = Validator.validate(llm_output, rule_body, taboos)
        
        if is_valid:
            return llm_output, f"LLM Drafted ({latency:.2f}s)"
        else:
            return rule_body, f"LLM Rejected: {reason} (Fallback to Rules)"
            
    except Exception as e:
        return rule_body, f"LLM Error: {str(e)} (Fallback to Rules)"
