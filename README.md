# Team Vedant: Vera LLM Engagement Engine

## Our Approach: The Hybrid "Single-Pass" Architecture
We built Vera to be a high-frequency, low-friction engagement engine. Early in development, we experimented with a multi-agent workflow (Extractor -> Strategist -> Copywriter -> Auditor). While the quality was high, the latency (~25s per message) caused massive timeouts when processing concurrent trigger batches. 

To solve this, we pivoted to a **Hybrid Rule-Based + Single-Pass LLM** architecture:
1. **Python as the Routing & Safety Gate**: `bot.py` handles all the deterministic work. It manages the state machine, evaluates hostility/auto-replies, and maps triggers to specific baseline templates. For complex triggers (like `research_digest`), Python explicitly looks up the cited source from the Category digest and passes it forward.
2. **The "Super-Prompt" Execution**: We consolidated all drafting logic into a single, heavily constrained prompt in `llm_composer.py`. Rather than relying on post-generation validation, we enforce strict rules *during* generation (e.g., `MANDATORY DATES`, `TERMINAL HOOK RULE`).
3. **Engagement Compulsion over Passive Notification**: We strictly instruct the LLM to use psychological levers. A message is never just "Your views are up"; it is framed with **Loss Aversion** ("Don't let these potential patients drop off") and anchored to the merchant's specific **Active Offer**.

For multi-turn conversations (`/v1/reply`), we maintain the same philosophy. Python heuristic functions (e.g., `looks_action_intent`) determine the "Policy Intent" (e.g., `ACTION_COMMITMENT` vs `DE_ESCALATION`), and the LLM is simply tasked with naturalizing that intent given the conversation history.

## Tradeoffs Made
*   **Latency vs. Self-Correction**: We deliberately removed the LLM "Auditor" pass. The tradeoff is that if the LLM hallucinates a metric, it ships. We mitigated this risk by strictly filtering the context passed to the LLM (e.g., passing exact strings like `calls` instead of allowing the LLM to infer `leads`), accepting occasional clunky formatting over multi-pass latency.
*   **Hardcoded Intent Recognition**: For conversational replies, we rely on regex and keyword matching to detect intent (e.g., "stop", "yes") rather than an LLM intent classifier. This sacrifices some nuance in understanding complex merchant replies in exchange for zero-latency safety and guaranteed state transitions.
*   **Code-Mixing Logic**: We rely on simple heuristics to detect the user's language preference. While a dedicated LLM call could perfectly detect dialect nuances, passing a hardcoded `mix_lang` constraint to the drafting LLM proved to be 95% as effective at a fraction of the cost.

## What Additional Context Would Have Helped
1.  **Merchant Communication History**: Knowing *how* the merchant typically replies (e.g., do they prefer short texts, voice notes, or formal emails?) would have allowed us to adjust Vera's outgoing register dynamically, rather than relying solely on the broad Category voice.
2.  **Customer LTV (Lifetime Value)**: For customer-facing triggers (`winback`, `recall_due`), having an explicit LTV or "Loyalty Tier" in the payload would have allowed us to dynamically alter the incentive (e.g., offering a 20% discount to a VIP vs. a standard reminder to a casual visitor).
3.  **Real-Time Competitor Benchmarks**: While we had static peer averages, real-time context like "Your CTR dropped below the neighborhood average *today*" would have made the Loss Aversion lever significantly more potent.
