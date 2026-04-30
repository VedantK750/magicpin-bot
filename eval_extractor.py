import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Import the judge_simulator components
# We need to make sure the directory is in path if it's not
sys.path.append(str(Path(__file__).parent))

import judge_simulator

def run_extraction():
    print("Initializing Extraction...")
    
    # Initialize provider from judge_simulator's config
    try:
        provider = judge_simulator.create_provider()
    except Exception as e:
        print(f"Error creating provider: {e}")
        return

    # Initialize client, dataset and scorer
    client = judge_simulator.BotClient(judge_simulator.BOT_URL)
    dataset = judge_simulator.DatasetLoader(judge_simulator.DATASET_DIR)
    
    if not dataset.load():
        print("Failed to load dataset.")
        return

    scorer = judge_simulator.LLMScorer(provider, dataset)
    
    # Check if bot is alive
    _, err, _ = client.healthz()
    if err:
        print(f"Bot is not reachable at {judge_simulator.BOT_URL}: {err}")
        return

    # Pushing context (required for evaluation)
    print("Pushing context...")
    for slug, cat in dataset.categories.items():
        client.push_context("category", slug, 1, cat)
    for mid, m in dataset.merchants.items():
        client.push_context("merchant", mid, 1, m)
    for tid, t in dataset.triggers.items():
        client.push_context("trigger", tid, 1, t)

    tids = list(dataset.triggers.keys())
    results = []

    print(f"Starting evaluation of {len(tids)} triggers...")
    
    # Evaluate in batches like the judge_simulator does
    for i in range(0, len(tids), 5):
        batch = tids[i:i+5]
        data, err, _ = client.tick(batch)
        if err:
            print(f"Tick failed for batch {i//5 + 1}: {err}")
            continue
        
        actions = data.get("actions", [])
        for action in actions:
            tid = action.get("trigger_id", "")
            mid = action.get("merchant_id", "")
            cid = action.get("customer_id")

            trigger = dataset.triggers.get(tid, {})
            merchant = dataset.merchants.get(mid, {})
            customer = dataset.customers.get(cid) if cid else None
            category = dataset.categories.get(merchant.get("category_slug", ""), {})

            print(f"Scoring message for trigger {tid}...")
            score = scorer.score(action, category, merchant, trigger, customer)
            
            results.append({
                "trigger_id": tid,
                "merchant_id": mid,
                "full_message": action.get("body", ""),
                "score_total": score.total,
                "details": {
                    "specificity": score.specificity,
                    "category_fit": score.category_fit,
                    "merchant_fit": score.merchant_fit,
                    "decision_quality": score.decision_quality,
                    "engagement": score.engagement_compulsion,
                    "penalties": score.penalties
                },
                "rationale": score.specificity_reason + " " + score.category_fit_reason + " " + score.merchant_fit_reason
            })

    # Save to file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"evaluation_results_{timestamp}.txt"
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"EVALUATION RUN - {datetime.now().isoformat()}\n")
        f.write("="*80 + "\n\n")
        
        for res in results:
            f.write(f"TRIGGER: {res['trigger_id']}\n")
            f.write(f"MERCHANT: {res['merchant_id']}\n")
            f.write(f"SCORE: {res['score_total']}/50\n")
            f.write(f"BREAKDOWN: Spec:{res['details']['specificity']}, Cat:{res['details']['category_fit']}, Merch:{res['details']['merchant_fit']}, Dec:{res['details']['decision_quality']}, Eng:{res['details']['engagement']}, Pen:-{res['details']['penalties']}\n")
            f.write("-" * 40 + "\n")
            f.write("MESSAGE:\n")
            f.write(f"{res['full_message']}\n")
            f.write("-" * 40 + "\n")
            f.write(f"RATIONALE: {res['rationale']}\n")
            f.write("\n" + "="*80 + "\n\n")

    print(f"Extraction complete. Results saved to {filename}")

if __name__ == "__main__":
    run_extraction()
