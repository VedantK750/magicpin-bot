import json
import os
from pathlib import Path

def repair(dataset_dir: Path):
    merchants_dir = dataset_dir / "merchants"
    for f in merchants_dir.glob("*.json"):
        with open(f, 'r+') as fp:
            m = json.load(fp)
            
            # Repair Offers
            if not m.get("offers"):
                cat = m.get("category_slug")
                if cat == "dentists":
                    m["offers"] = [{"title": "Dental Cleaning", "price": "₹299", "status": "active"}]
                elif cat == "salons":
                    m["offers"] = [{"title": "Haircut", "price": "₹99", "status": "active"}]
                elif cat == "restaurants":
                    m["offers"] = [{"title": "Combo Deal", "price": "₹199", "status": "active"}]
                elif cat == "gyms":
                    m["offers"] = [{"title": "Trial Class", "price": "FREE", "status": "active"}]
                elif cat == "pharmacies":
                    m["offers"] = [{"title": "Health Check", "price": "FREE", "status": "active"}]
            
            # Repair Review Themes
            if not m.get("review_themes"):
                m["review_themes"] = ["Great service", "Prompt response", "Value for money"]
                
            fp.seek(0)
            json.dump(m, fp, indent=2)
            fp.truncate()

if __name__ == "__main__":
    repair(Path("./surprise_dataset"))
    print("Dataset repaired.")
