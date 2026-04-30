# magicpin AI Challenge — Implementation Plan

**Owner**: Team working on `bot.py`  
**Date**: 2026-04-30  
**Purpose**: Preserve complete implementation context so execution can continue without losing requirements, constraints, and decisions.

---

## 1. Mission

Build a stateful HTTP bot that:
- Accepts context pushes across `category`, `merchant`, `customer`, `trigger`.
- Decides proactive sends on `/v1/tick`.
- Handles reply turns on `/v1/reply`.
- Produces high-scoring, context-grounded message composition with strict operational reliability.

Primary scoring objective:
- Maximize the 5 quality dimensions (specificity, category fit, merchant fit, trigger relevance, engagement compulsion).
- Avoid all operational and anti-pattern penalties.

---

## 2. Canonical Inputs We Must Honor

Primary docs:
- `challenge-brief.md`
- `challenge-testing-brief.md`
- `examples/case-studies.md`
- `examples/api-call-examples.md`

Data inputs:
- `dataset/categories/*.json` (5 category contexts)
- `dataset/merchants_seed.json` (+ expanded to 50 merchants)
- `dataset/customers_seed.json` (+ expanded to 200 customers)
- `dataset/triggers_seed.json` (+ expanded to 100 triggers)
- `dataset/generate_dataset.py`
- `expanded/test_pairs.json` (30 canonical pairs when generated)

Local evaluator:
- `judge_simulator.py`

---

## 3. Critical Contracts (Non-Negotiable)

### Endpoints
- `GET /v1/healthz`
- `GET /v1/metadata`
- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`

### Timeouts and limits
- Respond within 30s hard cap for `/v1/tick` and `/v1/reply` (judge drops late responses).
- Internal SLO target: <=10s for `/v1/tick` and `/v1/reply` to stay within example latency budgets.
- `/v1/tick` action count max: 20.
- `/v1/context` payload size cap: 500 KB.
- Judge -> bot request rate can be up to 10 req/s.
- Warmup health checks must remain stable.

### Warmup/base-load expectations
- During warmup, judge pushes:
  - 5 categories
  - 50 merchants
  - 200 customers
  - 0 triggers initially
- Warmup pass condition in test brief: `contexts_loaded` reflects all 255 base contexts.
- During active test window, trigger contexts arrive incrementally (up to 100 in base dataset generation).

### Operational failure thresholds (from testing brief/examples)
- Healthz non-200 three consecutive times -> disqualification for that slot (operational penalty path).
- `/v1/tick` timeout (>30s) -> tick skipped, penalty applied.
- `/v1/reply` timeout (>30s) -> bot marked silent for that turn, penalty applied.
- Malformed action payload -> action scored 0 plus penalty.
- Repeated verbatim body in same conversation -> anti-repetition penalty.

### Context versioning
- Idempotency on `(scope, context_id, version)`.
- Same version re-push must not overwrite.
- Higher version replaces atomically.
- Stale version should return conflict shape.

### Message output contract
For each action in `/v1/tick`:
- `conversation_id`
- `merchant_id`
- `customer_id`
- `send_as`
- `trigger_id`
- `template_name`
- `template_params`
- `body`
- `cta`
- `suppression_key`
- `rationale`

For `/v1/reply`:
- `action`: one of `send | wait | end`.
- If `send`: include `body`, `cta`, `rationale`.
- If `wait`: include `wait_seconds`, `rationale`.
- If `end`: include `rationale`.

### Session-window/template contract
- Per brief: first outbound in WhatsApp 24h session window should use approved template-style fields.
- Implementation must always populate `template_name` and `template_params` for `/v1/tick` actions.
- Free-form body still required and judged; template fields are part of operational correctness.

---

## 4. Guardrails and Penalty Avoidance

### Must avoid
- Fabricated data not present in contexts.
- Generic vague offers when service+price is available.
- Multiple CTAs in a single outbound.
- Repeated verbatim message in same conversation.
- Ignoring language preference.
- Tone mismatch by category.
- Long preamble and re-introductions.
- Mishandling explicit merchant intent transition.
- Spamming despite no clear trigger value.

### Grounding-first non-hallucination rule (hard)
- Every concrete claim in `body` must be traceable to:
  - current `trigger.payload`, or
  - current `merchant` context, or
  - current `category` context, or
  - current `customer` context (when scope is customer), or
  - deterministic arithmetic transformation of those values.
- If a value cannot be traced, do not include it.
- No invented competitor names, study sources, percentages, dates, slots, counts, prices, or operational promises.

### URL policy decision
There is a document inconsistency:
- Main brief: URLs allowed when useful.
- API examples failure mode: URL in body marked hard fail.

Implementation choice:
- **Disallow URLs in `body` by default** to stay safe against hard-fail interpretation.

### Reply-flow safety
- Auto-reply detection with escalation:
  - First clear auto-reply: one short owner-targeted nudge.
  - Repeated auto-reply: `wait`.
  - Repeated again: `end`.
- Hard opt-out / hostile: immediate graceful `end`.
- Explicit action intent (“let’s do it”, “go ahead”, “what’s next”): switch to action mode, no re-qualification.

### Canonical stop conditions
- Stop immediately (`action = end`) on explicit not-interested/opt-out.
- Stop after 3 unanswered nudges in same conversation thread.
- Stop after repeated auto-reply progression reaches terminal step.

---

## 5. Architecture Blueprint

### 5.1 In-memory state (v1)
- `contexts[(scope, context_id)] = {version, payload, delivered_at}`
- `conversations[conversation_id] = ConversationState`
- `suppression_sent[(merchant_id, suppression_key)] = last_sent_at`
- `trigger_status[trigger_id] = {last_decision, last_conversation_id}`
- `message_history[conversation_id] = set(normalized_body_hashes)`

Optional persistence upgrade:
- SQLite/Redis adapter with same interface.

### 5.2 Domain models
- `CategoryContext`
- `MerchantContext`
- `CustomerContext`
- `TriggerContext`
- `ComposedAction`
- `ReplyDecision`
- `ConversationState`

Use typed validation for each incoming payload and internal normalized defaults.

### 5.3 Modules
- `app.py` or `bot.py`: FastAPI routes.
- `store.py`: context + conversation storage.
- `schemas.py`: request/response models.
- `resolver.py`: fetch + bind category/merchant/customer/trigger into compose input.
- `composer.py`: compose outbound message.
- `reply_policy.py`: handle inbound merchant/customer reply.
- `validators.py`: output compliance checks.
- `dedup.py`: suppression and anti-repetition.
- `language.py`: language selection heuristics.
- `templates.py`: first-touch template name/params policy.
- `telemetry.py`: structured logs and counters.

---

## 6. Composition Strategy (How We Generate High-Score Messages)

### 6.1 Inputs to composer
- Category voice + taboos + offer catalog + digest + peer stats + trends.
- Merchant identity + performance + offers + signals + history + aggregates.
- Trigger kind/source/payload/urgency/suppression.
- Optional customer identity/relationship/state/preferences/consent.

### 6.2 Output shape
- `body`
- `cta`
- `send_as`
- `suppression_key`
- `rationale`
- `template_name`
- `template_params`

### 6.3 Message quality rubric mapping
Specificity:
- Include concrete facts from payload/context: counts, deltas, dates, offer prices, named digest item source.

Category fit:
- Enforce category-specific tone and taboo list.

Merchant fit:
- Prefer owner first name if present.
- Reference merchant’s real signals/offers/performance numbers.

Trigger relevance:
- First sentence should establish “why now”.
- Explicitly connect to trigger kind.

Engagement compulsion:
- One low-friction next step.
- Loss aversion or reciprocity or curiosity where appropriate.

### 6.6 Claim provenance matrix (explicit)
- Research/compliance claims:
  - Must include source only if present in `category.digest[*].source` or trigger payload reference.
  - If source absent, avoid citation-like phrasing.
- Performance claims:
  - Use only `merchant.performance` and `merchant.performance.delta_7d`.
  - Peer comparison only if `category.peer_stats` has corresponding metric.
- Offer claims:
  - Use active merchant offers first (`merchant.offers[].status == active`).
  - Fallback to category offer catalog only when merchant-specific offer is unavailable and wording reflects suggestion, not “already active”.
- Customer scheduling claims:
  - Use trigger/customer-provided slot/date fields directly.
  - If slot unavailable, ask for preferred time instead of inventing slots.
- Aggregate counts:
  - Use only fields present in `merchant.customer_aggregate`.
  - Never infer exact cohort counts from percentages alone unless arithmetic is explicitly shown and inputs exist.

### 6.7 Allowed deterministic transformations
- Allowed:
  - Convert decimals to percentages (e.g., `0.021` -> `2.1%`).
  - Compute simple deltas where both operands are present.
  - Convert ISO date/time to readable label without changing date meaning.
  - “days until” derived from explicit date fields.
- Not allowed:
  - Creating missing baselines, missing cohorts, missing benchmarks.
  - Extrapolating forecast metrics not present in contexts.
  - Naming entities (competitors, sources, institutions) absent from contexts.

### 6.8 Performance and numeric wording rules
- Never state an exact `x%` drop/rise unless exact value exists in:
  - `trigger.payload`, or
  - `merchant.performance` / `merchant.performance.delta_7d`.
- If trigger kind implies a change (e.g., `perf_dip`) but exact numeric payload is missing:
  - say “performance dip signal” (qualitative),
  - do not invent `%` or absolute count.
- For peer comparisons:
  - include peer metric only when that specific metric exists in `category.peer_stats`.
  - include both merchant value and peer value when both are present.

### 6.4 CTA policy
- Action triggers: binary or concise open-ended ask.
- Customer booking reminders: allow multi-choice slot CTA.
- Info-only triggers: `cta = none` acceptable.

### 6.5 Send-as policy
- `scope == merchant` -> `send_as = vera`
- `scope == customer` -> `send_as = merchant_on_behalf`

### 6.9 Per-turn language adaptation policy
- Canonical requirement: merchant/customer may switch language mid-conversation; bot must adapt per turn.
- Language priority order per outbound turn:
  1. latest inbound reply language signal (current turn),
  2. `customer.identity.language_pref` (for customer scope),
  3. `merchant.identity.languages`,
  4. default English.
- If latest inbound and profile language disagree, prefer latest inbound (conversation-local override).
- Maintain transliterated Hindi-English code-mix when signals indicate `hi-en mix`.
- Do not hard-lock language from first turn; reevaluate every `/v1/reply` turn.

---

## 7. Trigger Routing Plan

Implement per-kind strategy function with safe fallback.

Known kinds in data:
- `research_digest`
- `regulation_change`
- `recall_due`
- `perf_dip`
- `renewal_due`
- `festival_upcoming`
- `wedding_package_followup`
- `curious_ask_due`
- `winback_eligible`
- `ipl_match_today`
- `review_theme_emerged`
- `milestone_reached`
- `active_planning_intent`
- `seasonal_perf_dip`
- `customer_lapsed_hard`
- `trial_followup`
- `supply_alert`
- `chronic_refill_due`
- `category_seasonal`
- `gbp_unverified`
- `cde_opportunity`
- `competitor_opened`
- `perf_spike`
- `dormant_with_vera`
- plus generated kinds with placeholder payload (`appointment_tomorrow`, `customer_lapsed_soft`, etc.)

Routing behavior:
- Specialized handlers for high-value seeds and replay-sensitive kinds.
- Generic fallback for placeholder payloads:
  - Avoid fabricating unavailable details.
  - Ask one grounded next step based on merchant data + trigger kind name.

---

## 8. Reply Policy Plan

### 8.1 Auto-reply detection
Signals:
- Exact known canned patterns.
- Repeated near-identical response text.
- Business auto-response phrases.

Stateful progression:
- Attempt 1: short owner-directed clarification send.
- Attempt 2: `wait` with deterministic backoff (default `wait_seconds = 14400`).
- Attempt 3: `end` to prevent wasted turns.

### 8.2 Intent transition
Commitment lexicon:
- “yes”, “let’s do it”, “go ahead”, “what’s next”, “start”, “send it”.

On detect:
- Move to action artifact (draft/send/confirm), not more qualification.

### 8.3 Hostile / stop intent
If message contains stop/frustration/opt-out:
- immediate `end` with rationale.

### 8.4 Off-topic curveball
- Politely decline out-of-scope.
- Redirect to active trigger objective.

### 8.5 Anti-repetition on replies
- Hash body text and block exact repeats within same conversation.
- If candidate body repeats, regenerate via alternative phrasing strategy.

### 8.6 Unanswered nudge exit policy
- Track `nudge_count` per conversation when bot sends but receives no meaningful engagement.
- After 3 unanswered nudges on the same conversation thread, return `action = end` gracefully.
- Suppress further proactive sends for that conversation unless a fresh qualifying trigger/version arrives.

Definition of “meaningful engagement”:
- Any non-auto-reply merchant/customer response that is not empty and not pure boilerplate acknowledgment.
- Explicit action intent, question, objection, or preference counts as engagement and resets unanswered streak.

---

## 9. Data Realities We Must Handle

### 9.1 Expanded dataset contains placeholders
Generated triggers frequently include:
- `payload = {"placeholder": true, "metric_or_topic": "<kind>"}`.

Implication:
- Must degrade gracefully without invented details.
- Use merchant/category facts instead of missing payload specifics.

Example-safe behavior for placeholder payload:
- Good: “I noticed a `perf_dip` signal for your listing; want me to draft a 2-step recovery post using your active offer?”
- Bad: “Your calls dropped exactly 37% yesterday” (if not present).

### 9.2 Variable merchant/customer schemas
Some fields differ across categories/seed vs generated records.

Plan:
- Robust helper getters with defaults.
- Optional-field-safe formatting functions.
- Never assume presence of slots, offers, or aggregate subfields.

### 9.3 Consent and customer scope
For customer-scope triggers:
- If customer context missing or opt-in false, conservative behavior:
  - skip send or issue merchant-facing action suggestion (configurable).

---

## 10. Master Phase Checklist (A -> G)

This is the canonical execution checklist across all phases, with current status.

### Phase A — Foundation and API Contract
Goal: satisfy transport contract and context lifecycle.

- [x] Implement `GET /v1/healthz`.
- [x] Implement `GET /v1/metadata`.
- [x] Implement `POST /v1/context` with version handling.
- [x] Implement `POST /v1/tick`.
- [x] Implement `POST /v1/reply`.
- [x] Implement optional `POST /v1/teardown`.
- [x] Add typed request models.
- [x] Add typed response shaping via route return dicts.
- [x] Track `contexts_loaded` counts by scope.
- [x] Return stale-version conflict behavior on context push.
- [x] Validate all response payloads against explicit Pydantic response models (strict output schema class enforcement).
- [x] Add comprehensive contract tests for all endpoint error branches (`400`, `409`, malformed body).

Phase A exit criteria:
- [x] Warmup-compatible behavior with `healthz/metadata/context` contract verified via smoke harness (`smoke_phase_ab_direct.py`).

### Phase B — Tick Engine, Context Binding, and Action Assembly
Goal: deterministic proactive action generation.

- [x] Resolve active trigger contexts from `available_triggers`.
- [x] Bind trigger -> merchant -> category -> optional customer.
- [x] Skip missing dependencies safely (no malformed output).
- [x] Respect trigger expiry check.
- [x] Enforce max 20 actions per tick.
- [x] Build deterministic `conversation_id`.
- [x] Populate required action fields, including `template_name` + `template_params`.
- [x] Attach `suppression_key`.
- [x] Add suppression dedup gating.
- [x] Add per-conversation anti-repetition hash check.
- [x] Return `{"actions": []}` when nothing should be sent.
- [x] Enforce explicit "one action per `(merchant_id, conversation_id)` pair per tick" invariant with dedicated guard.
- [x] Add tick-level elapsed-time guard that auto-short-circuits to empty actions under budget pressure.

Phase B exit criteria:
- [x] No malformed action payloads across sampled triggers (seed-like + generated placeholder-style triggers in smoke harness assertions).

### Phase C — Reply Engine and Conversation Policy
Goal: robust synchronous handling of inbound merchant/customer replies.

- [x] Create/retrieve conversation state on reply.
- [x] Handle explicit not-interested/opt-out with graceful `end`.
- [x] Handle auto-reply progression (`send` -> `wait` -> `end`).
- [x] Handle intent-transition to action mode (no re-qualification).
- [x] Handle off-topic redirection.
- [x] Prevent repeated response body in same conversation.
- [x] Track meaningful engagement state.
- [x] Implement 3 unanswered nudges stop behavior.
- [x] Add stricter conversation closure suppression logic so ended conversations cannot be reopened without fresh qualifying trigger/version. (reply path + nudge-state closure)
- [x] Add reply-text normalization pipeline (unicode cleanup, punctuation stripping, whitespace collapse, transliteration-friendly matching).
- [x] Implement rule-based hostile/opt-out detector with confidence tiers (`high`, `medium`, `low`) and deterministic action mapping.
- [x] Expand hostile-language detection lexicon:
  - [x] English core phrases (stop/abuse/frustration/escalation).
  - [x] Hindi (Roman + Devanagari) variants.
  - [x] Additional Indic packs (Tamil, Telugu, Kannada, Marathi, Bengali, Malayalam, Gujarati, Punjabi) starter phrases for high-frequency opt-out/hostile intent.
- [x] Add explicit opt-out hard-stop matcher that triggers immediate `end` irrespective of language pack.
- [x] Add mild-frustration de-escalation path (single apology/clarification) before terminal close on repeat.
- [x] Add stateful hostility escalation: repeated medium/high hostility across turns -> faster termination.
- [x] Add fixture-based multilingual tests (`phrase -> expected action`) for reply policy regression safety (`smoke_phase_c_direct.py`).
- [x] Add dual-mode auto-reply handling to reconcile example variants:
  - mode A: first auto-reply -> immediate `wait`,
  - mode B: first auto-reply -> one nudge `send`, then `wait`, then `end`,
  - deterministic selector based on conversation stage/config (`AUTO_REPLY_FIRST_ACTION` + stage default in `bot.py`).
- [x] Add conversation-aware artifact fulfillment for engaged replies:
  - if merchant asks for concrete artifacts in one turn (e.g., “send abstract + draft”), return useful artifact content immediately instead of pure confirmation copy.
  - validated in `smoke_phase_c_direct.py` with same-turn artifact assertion.
- [x] Run and lock replay-scenario verification via simulator:
  - `auto_reply_hell`
  - `intent_transition`
  - `hostile`
  - plus evidence logs for policy-branch correctness (`smoke_phase_c_replay_simulator.py` -> `phase_c_replay_evidence.json`).

Phase C exit criteria:
- [x] Replay-like policy behavior confirmed in simulator scenarios (`smoke_phase_c_replay_simulator.py`).
- [x] Multilingual hostile/opt-out fixtures pass with deterministic outcomes (`smoke_phase_c_direct.py`).

### Phase D — Quality, Personalization, and Language Adaptation
Goal: maximize rubric scores (specificity/category fit/merchant fit/trigger relevance/engagement).

- [x] Merchant personalization helpers (owner/merchant identity).
- [x] Trigger-first framing generation paths.
- [x] Customer-branch composition.
- [x] CTA classification and shaping.
- [x] Rationale generation.
- [x] Basic language preference handling (`customer.language_pref` and merchant language fallback).
- [ ] Full category voice/taboo enforcement from `category.voice`.
- [ ] Per-turn language switch detection using latest inbound reply as primary signal (requirement; implemented via Phase G LLM detector with deterministic fallback).
- [ ] Language consistency validator in output gate (reject/repair mismatch).
- [ ] More category-specific compulsion strategies (social proof / reciprocity / curiosity families).
- [ ] Implement specialized composers to eliminate internal jargon ("dormant with vera", "gbp unverified") and use natural merchant-facing labels.
- [ ] Implement trigger-kind deep composers for high-value kinds currently on generic fallback:
  - [ ] `ipl_match_today`
  - [ ] `review_theme_emerged` (fix sentiment hallucination - don't call negative reviews positive)
  - [ ] `milestone_reached` (inject exact metric/value)
  - [ ] `category_seasonal` (unpack payload trends)
  - [ ] `gbp_unverified` (explain value uplift)
  - [ ] `competitor_opened` (include their offer price)
  - [ ] `dormant_with_vera` (natural re-engagement)
  - [ ] `winback_eligible`
  - [ ] `renewal_due`
- [ ] Upgrade research/compliance compositions with payload-grounded specifics:
  - `supply_alert`: include batch IDs/manufacturer/impacted cohort/replacement workflow when present.
  - `regulation_change`: include effective date + exact compliance delta when present.
- [ ] Improve customer-scope copy quality:
  - natural-language rendering for machine-style tokens (e.g., `6_month_cleaning`),
  - higher-fidelity relationship-state personalization (lapse duration/goals/visit context),
  - preserve language preference naturally (no awkward forced inserts).
- [ ] Add grammar/style repair pass for composed body (e.g., "calls is" -> "calls are").

Phase D exit criteria:
- [ ] Message quality no longer mostly fallback-style for placeholder payloads; improved specificity without hallucination.

### Phase E — Safety, Privacy, and Operational Hardening
Goal: avoid penalties and operational failures.

- [x] URL rejection in outbound body.
- [x] Anti-repetition checks.
- [x] Basic schema presence checks for actions.
- [x] Teardown state wipe endpoint.
- [ ] Add explicit timeout guardrails for `/v1/tick` and `/v1/reply` compute path.
- [ ] Add structured request logging for failure diagnosis.
- [ ] Add resilience for malformed incoming payload fields (defensive normalization path).
- [ ] Enforce privacy rule: no outbound non-LLM external API calls carrying merchant/customer data (document + code guard).
- [ ] Add health degradation alerts/counters for repeated failures.
- [ ] Enforce strict provenance allowlist as hard gate in validation pipeline (numbers/dates/sources/entities/price mentions must be context-traceable).
- [ ] Add explicit validator gate order for every outbound candidate:
  - schema check,
  - provenance/no-hallucination check (numeric/date literals must exist in context),
  - taboo/tone safety check,
  - URL/repetition checks,
  - fail closed if any gate fails.

Phase E exit criteria:
- [ ] Stable handling for malformed inputs and no obvious penalty-triggering failures in dry runs.

### Phase F — Submission and Packaging
Goal: produce challenge deliverables.

- [ ] Generate expanded dataset and canonical `test_pairs.json` in reproducible workflow.
- [ ] Build `submission.jsonl` generator for 30 canonical pairs.
- [ ] Ensure exactly 30 lines, one per `test_id`, deterministic ordering.
- [ ] Add concise `README.md` (<= 1 page) with approach/tradeoffs.
- [ ] Optionally add `conversation_handlers.py` for multi-turn demo/tiebreaker.
- [x] Add `requirements.txt`.
- [x] Create local `.venv` and install runtime deps.
- [ ] Add run instructions for local + deployed modes.
- [ ] Add quality regression harness over all 25 seed triggers with per-dimension thresholds and fail-fast reporting.
- [ ] Run simulator scenarios and capture baseline score artifacts:
  - warmup
  - phase2 short
  - auto-reply hell
  - intent transition
  - hostile/off-topic
  - full evaluation

Phase F exit criteria:
- [ ] Artifact bundle ready: `bot.py`, `submission.jsonl`, `README.md` (+ optional handlers).

### Phase G — LLM Integration (Hybrid Architecture)
Goal: improve composition quality while preserving deterministic/safe behavior.

- [ ] Adopt strict hybrid runtime contract:
  - **Rule-First Policy:** Rules exclusively decide the action (`send|wait|end`, opt-out, hostility, auto-reply, stop conditions).
  - **LLM-First Drafting:** LLM is used *only* for content drafting (body/CTA phrasing, specificity) on `send` turns.
  - **Rule-First Safety:** Validator checks act as a hard gate before any LLM output is sent (schema, provenance, taboo, URL, repetition).
  - **Rule-First Fallback:** Safe fallback to rule-based composer templates if the LLM errors, times out, or fails the validator.
- [ ] Decide provider abstraction (`OpenAI/Anthropic/Gemini/DeepSeek/...`) behind a pluggable interface.
- [ ] Add provider config via env vars (`LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, timeout).
- [ ] Implement LLM drafting path for `send` content with structured output contract (`body`, `cta`, `rationale`) and strict timeout budget.
- [ ] Implement prompt templates for major trigger families.
- [ ] Implement per-turn language switch detector from latest inbound text (LLM primary), with deterministic fallback to profile language when LLM is unavailable/low-confidence.
- [ ] Add strict post-generation validator + repair loop (1 retry max).
- [ ] Keep deterministic mode toggle (`RULE_BASED_ONLY=true`) for fallback/reliability testing.
- [ ] Force low temperature and stable prompt structure.
- [ ] Add response budget controls (token/time caps).
- [ ] Add redact/no-leak policy in prompt assembly for non-required fields.
- [ ] Add A/B switch by trigger kind for experimentation.

Phase G exit criteria:
- [ ] LLM path demonstrably improves quality on simulator scoring without violating safety/latency/replay constraints.

---

## 11. Canonical Compliance Checklist (Brief + Testing Brief)

This checklist is a direct cross-check against challenge documentation to prevent missed requirements.

### Contract and lifecycle
- [x] 5 required endpoints implemented.
- [x] Context idempotency/stale-version behavior implemented.
- [x] Stateful memory across calls implemented.
- [x] Optional teardown wipe implemented.
- [x] Full warmup count validation tested end-to-end (5 category, 50 merchant, 200 customer) via `smoke_phase_ab_direct.py`.

### Composition requirements
- [x] Include template fields in tick actions.
- [x] Single primary CTA policy in composer.
- [x] `send_as` uses scope (`vera` vs `merchant_on_behalf`).
- [ ] Category taboo enforcement complete.
- [ ] Per-turn language adaptation complete.
- [ ] Specificity/source anchoring strengthened for all trigger families.

### Anti-pattern / penalty prevention
- [x] URL blocked.
- [x] Anti-repetition guard.
- [x] Graceful end on opt-out / hostility.
- [x] Auto-reply handling progression.
- [x] Intent transition behavior.
- [ ] Strict no-hallucination provenance validator complete.
- [ ] Timeout budget guard complete.

### Testing and evaluation readiness
- [ ] Run warmup simulator scenario.
- [ ] Run short phase-2 scenario.
- [ ] Run auto-reply hell scenario.
- [ ] Run intent-transition scenario.
- [ ] Run hostile/off-topic scenario.
- [ ] Run full evaluation scenario and collect baseline score.
- [ ] Add regression checklist for failures discovered in simulator output.

### Deployment and submission readiness
- [ ] Public URL deployment path validated.
- [ ] `submission.jsonl` generation path validated.
- [ ] README final draft completed.
- [ ] Operational checklist completed (quota/timeouts/health stability).

---

## 12. Determinism and Latency Strategy

Determinism:
- Prefer rule-based policy decisions first (`send|wait|end` and safety branches).
- If LLM path is used for `send` drafting, force low temperature and stable prompt layout.
- Seeded tie-breakers for trigger ordering.

Latency:
- O(1) store lookups by key.
- Minimal per-tick processing; skip invalid/unresolvable triggers fast.
- Hard timeout fallback: return `{"actions": []}` if composing risks overrun.
- Never block `/v1/tick` waiting for expensive retry loops.

---

## 13. Validation Pipeline (Before Returning Any Message)

For each candidate outbound:
1. Required fields present.
2. `send_as` matches trigger scope.
3. `merchant_id` and optional `customer_id` consistent with trigger.
4. `suppression_key` non-empty and trigger-consistent.
5. `cta` in allowed enum.
6. Body non-empty for send actions.
7. Body has no URL.
8. Body not repeated in conversation.
9. Body does not violate obvious category taboos.
10. Rationale references the same decision basis as body.
11. Every number/date/source/entity mention in body passes provenance check against current contexts.
12. For placeholder trigger payloads, body contains no fabricated payload-specific specifics.

If validation fails:
- Attempt single constrained regeneration (LLM repair when LLM mode is enabled; otherwise deterministic template fallback).
- If still failing, skip action rather than returning malformed output.

Provenance check implementation note:
- Build extracted token sets for:
  - numeric literals
  - date/time mentions
  - source names / competitor names
  - offer price mentions
- Validate against context-derived allowlist before send.

---

## 14. Conversation State Model

Store per conversation:
- `conversation_id`
- `merchant_id`
- `customer_id`
- `trigger_id`
- `created_at`
- `last_turn_number`
- `last_bot_body`
- `auto_reply_count`
- `ended`
- `suppressed`
- `message_hashes`
- `intent_state` (`qualifying|actioning|closed`)

---

## 15. Logging and Debug Plan

Structured logs per request:
- request id
- endpoint
- latency ms
- trigger ids considered
- actions returned count
- reason for skipped triggers
- validation failures
- reply policy branch selected

Purpose:
- quick diagnosis of low score and operational penalties.

---

## 16. Risk Register and Mitigation

Risk: Schema drift between docs and real payloads.  
Mitigation: tolerant parsers + defaults + strict output validator.

Risk: Placeholder trigger payloads causing hallucinations.  
Mitigation: explicit placeholder detector and conservative copy.

Risk: Inconsistent URL guidance across documents.  
Mitigation: global URL block in body.

Risk: Auto-reply false positives.  
Mitigation: confidence thresholds + staged progression (send -> wait -> end).

Risk: Timeout under batch ticks.  
Mitigation: max actions cap + early exit and cheap routing.

Risk: Repetition penalties.  
Mitigation: conversation-local body hashing and variant fallback.

---

## 17. Deliverable Plan

Planned files:
- `bot.py` (or `app.py`) with endpoint server.
- `implementation_plan.md` (this file, source of truth).
- `submission.jsonl` generator utility.
- `README.md`.
- Optional `conversation_handlers.py`.

---

## 18. Execution Order (Immediate Next Steps)

1. Build endpoint skeleton and schema models.
2. Implement context storage/versioning + healthz/metadata.
3. Implement tick orchestration with action validator.
4. Implement reply policy state machine.
5. Add composition quality rules and trigger-specific routing.
6. Run judge simulator scenarios and iterate on weak dimensions.
7. Generate submission artifacts.

---

## 19. Done Definition

System is “ready” when:
- All endpoints return valid responses under contract.
- Warmup passes reliably with correct context counts.
- No malformed outputs in simulator.
- Replay behaviors satisfy auto-reply, intent transition, hostile handling.
- Composition outputs are specific, category-correct, merchant-grounded, trigger-relevant, and engaging.
- No unverifiable numeric/source/entity claims in outbound bodies under sampled replay + batch tests.
- `submission.jsonl` can be generated deterministically for 30 canonical test pairs.

---

## 20. Strict Compliance Audit (2026-04-30)

Audit basis:
- `examples/api-call-examples.md`
- `examples/case-studies.md`
- Live output sampling from current `bot.py` on all 25 seed triggers + replay probes.

### 20.1 API-call examples compliance (strict)

- [x] Endpoint surface and response shapes match (`/healthz`, `/metadata`, `/context`, `/tick`, `/reply`, `/teardown`).
- [x] Context version conflict behavior (`409 stale_version`) matches examples.
- [x] Tick action schema is complete (all required fields present).
- [x] URL-in-body hard block implemented.
- [x] Opt-out/hard-stop graceful `end` implemented.
- [x] Off-topic redirection behavior implemented.
- [x] Intent-transition behavior implemented (`yes/go ahead/what's next` -> action mode).
- [x] Auto-reply progression implemented (`send` -> `wait` -> `end`).
- [ ] Align auto-reply policy with both example variants:
  - Example 2.5 shows first auto-reply -> immediate `wait`.
  - Replay example 4.1 shows first auto-reply -> one nudge `send`.
  - Action: add config/heuristic switch to choose policy by conversation stage.
- [ ] Add stronger engaged-reply fulfillment (Example 2.4 style): when merchant asks for multiple artifacts in one turn, return concrete deliverable content instead of generic confirmation.
- [ ] Add simulator-backed confirmation for replay scenarios (`auto_reply_hell`, `intent_transition`, `hostile`, `phase2_short`, `full_evaluation`) and record baseline scores.

### 20.2 Case-study quality compliance (strict)

Strict scorecard against the 10 "good" anchor cases (0-50 each):
- Case 1 Dentists/research digest: **37/50**
- Case 2 Dentists/customer recall: **39/50**
- Case 3 Salons/wedding follow-up: **24/50**
- Case 4 Salons/curious ask: **27/50**
- Case 5 Restaurants/IPL day: **23/50**
- Case 6 Restaurants/active planning: **33/50**
- Case 7 Gyms/seasonal dip: **31/50**
- Case 8 Gyms/customer lapse: **26/50**
- Case 9 Pharmacies/supply alert: **20/50**
- Case 10 Pharmacies/chronic refill: **22/50**

Average strict quality score: **28.2/50** (operationally safe, quality still below top-tier target).

### 20.3 Top strict gaps to fix (priority checklist)

- [ ] Enforce category voice + taboo filtering from `category.voice` before send.
- [ ] Implement trigger-kind deep composers for high-value kinds still on generic fallback:
  - `ipl_match_today`
  - `review_theme_emerged`
  - `milestone_reached`
  - `category_seasonal`
  - `gbp_unverified`
  - `competitor_opened`
  - `dormant_with_vera`
  - `winback_eligible`
- [ ] Upgrade compliance/research handlers to include payload-grounded specifics:
  - For `supply_alert`: batch IDs, manufacturer, impacted cohort count (if present), replacement next step.
  - For `regulation_change`: effective date + exact compliance delta from payload/digest.
- [ ] Improve customer-scope quality:
  - replace raw tokens (`6_month_cleaning`) with natural language,
  - preserve language preference without awkward forced inserts,
  - include relationship-state facts (last visit/lapse duration/goals) when available.
- [ ] Add provenance validator for all number/date/source/entity mentions (hard gate pre-send).
- [ ] Add grammar/style repair pass for templated outputs (example: "your calls is down" -> "are down").
- [x] Add conversation-aware artifact generation in reply mode:
  - if inbound asks "send abstract + draft", return those artifacts in-body immediately.
- [ ] Strengthen compulsion levers by category (loss aversion, reciprocity, social proof, low-friction next step) rather than one generic CTA pattern.
- [ ] Add per-turn language adaptation based on latest inbound message signal using LLM detection with deterministic fallback.
- [ ] Add quality regression harness over all 25 seed triggers with per-dimension scoring and thresholds.
