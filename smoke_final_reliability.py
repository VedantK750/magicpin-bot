import json
import time
import requests
import asyncio
from concurrent.futures import ThreadPoolExecutor

# Bot Configuration
BOT_URL = "http://localhost:8080"
DATASET_PATH = "./dataset"

def load_json(path):
    with open(path) as f:
        return json.load(f)

async def run_smoke_test():
    print("🚀 Starting Final Reliability Smoke Test...")
    
    # 1. Clear state
    print("🧹 Wiping bot state...")
    requests.post(f"{BOT_URL}/v1/teardown")
    
    # 2. Push massive context
    print("📥 Pushing 10 merchants and 20 triggers...")
    merchants = load_json(f"{DATASET_PATH}/merchants_seed.json")["merchants"][:10]
    categories = ["dentists", "gyms", "pharmacies", "restaurants", "salons"]
    for cat in categories:
        c_data = load_json(f"{DATASET_PATH}/categories/{cat}.json")
        requests.post(f"{BOT_URL}/v1/context", json={
            "scope": "category", "context_id": cat, "version": 1, "payload": c_data, "delivered_at": "2026-05-02T10:00:00Z"
        })
    
    for m in merchants:
        requests.post(f"{BOT_URL}/v1/context", json={
            "scope": "merchant", "context_id": m["merchant_id"], "version": 1, "payload": m, "delivered_at": "2026-05-02T10:00:00Z"
        })
        
    triggers = load_json(f"{DATASET_PATH}/triggers_seed.json")["triggers"][:20]
    for t in triggers:
        requests.post(f"{BOT_URL}/v1/context", json={
            "scope": "trigger", "context_id": t["id"], "version": 1, "payload": t, "delivered_at": "2026-05-02T10:00:00Z"
        })

    # 3. Test Parallel Tick Throughput
    print(f"🔥 Testing parallel throughput with {len(triggers)} triggers in one tick...")
    start_time = time.monotonic()
    
    try:
        resp = requests.post(f"{BOT_URL}/v1/tick", json={
            "now": "2026-05-02T10:30:00Z",
            "available_triggers": [t["id"] for t in triggers]
        }, timeout=30)
        
        latency = time.monotonic() - start_time
        data = resp.json()
        actions = data.get("actions", [])
        
        print(f"✅ Tick finished in {latency:.2f}s")
        print(f"📊 Actions returned: {len(actions)}/20")
        
        if len(actions) > 2:
            print("🌟 SUCCESS: Parallel processing bypassed the old sequential bottleneck!")
        else:
            print("❌ FAILURE: Throughput is still too low.")

        # 4. Qualitative spot check on a few actions
        for i, act in enumerate(actions[:3]):
            print(f"\n--- Action {i+1} Spot Check ---")
            print(f"Trigger: {act['trigger_id']}")
            print(f"Body: {act['body'][:100]}...")
            print(f"Rationale: {act['rationale']}")

    except requests.exceptions.Timeout:
        print("❌ CRITICAL FAILURE: Tick timed out (>30s)")
    except Exception as e:
        print(f"❌ ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(run_smoke_test())
