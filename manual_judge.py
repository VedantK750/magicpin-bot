import json
import sys
from pathlib import Path

# Load judge simulator components
sys.path.append(str(Path(__file__).parent))
import judge_simulator

def test_message(custom_message: str):
    print("\n[INFO] Initializing Manual Judge Sandbox...\n")
    
    # 1. Initialize Provider
    try:
        provider = judge_simulator.create_provider()
    except Exception as e:
        print(f"Error creating provider: {e}")
        return

    # 2. Load dataset
    dataset = judge_simulator.DatasetLoader(Path(__file__).parent / "surprise_dataset")
    if not dataset.load():
        print("Failed to load dataset.")
        return

    scorer = judge_simulator.LLMScorer(provider, dataset)

    # 3. Setup Target Context
    tid = "trg_052_review_theme_emerged_m_033_lalit_restaura" # Using a restaurant trigger as an example
    mid = "m_033_lalit_restaurant_lucknow"
    
    trigger = dataset.triggers.get(tid, {})
    merchant = dataset.merchants.get(mid, {})
    category = dataset.categories.get("restaurants", {})
    
    # Ensure data exists to avoid KeyErrors
    if not merchant or not trigger or not category:
        print(f"Error: Could not find Context Data for {mid} or {tid}")
        return

    # 4. Construct Action Object
    action = {
        "trigger_id": tid,
        "merchant_id": mid,
        "send_as": "vera",
        "body": custom_message,
        "cta": "binary_yes_no", # Simulated CTA type
        "rationale": "Manual Sandbox Testing"
    }

    # 5. Score it
    print(f"--- TESTING MESSAGE ---")
    print(f"{custom_message}\n")
    print("--- JUDGE SCORING (Please Wait...) ---")
    
    score = scorer.score(action, category, merchant, trigger, None)
    
    print(f"Specificity      : {score.specificity}/10")
    print(f"Category Fit     : {score.category_fit}/10")
    print(f"Merchant Fit     : {score.merchant_fit}/10")
    print(f"Decision Quality : {score.decision_quality}/10")
    print(f"Engagement       : {score.engagement_compulsion}/10")
    print(f"Penalties        : -{score.penalties}")
    print(f"\nTOTAL SCORE      : {score.total}/50")
    print("\n--- RATIONALE ---")
    print(f"Specificity: {score.specificity_reason}")
    print(f"Category Fit: {score.category_fit_reason}")
    print(f"Merchant Fit: {score.merchant_fit_reason}")

if __name__ == "__main__":
    # You can edit this string to test different approaches!
    MY_MESSAGE = """Lalit, aapke Family Diner pe recent reviews mein ek positive theme aa raha hai. Log aapki service ko appreciate kar rahe hain, jisse aapki 4.0+ rating aur strong ho rahi hai. In reviews ke base par, kya main aapke Combo Deal @ ₹199 ko highlight karte hue ek 'Thank You' post draft kar doon jisse aur log attract hon?"""
    
    test_message(MY_MESSAGE)
