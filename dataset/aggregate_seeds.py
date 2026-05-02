import json
from pathlib import Path

def aggregate(dataset_dir: Path):
    for container in ["merchants", "customers", "triggers"]:
        data = {container: []}
        for f in (dataset_dir / container).glob("*.json"):
            with open(f) as fp:
                data[container].append(json.load(fp))
        
        with open(dataset_dir / f"{container.rstrip('s')}_seed.json", "w") as fp:
            json.dump(data, fp, indent=2)

if __name__ == "__main__":
    aggregate(Path("./surprise_dataset"))
    print("Aggregate seeds created.")
