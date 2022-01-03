import json
import os

_INDEX_PATH = "analysed/index.json"


def load_index():
    if os.path.exists(_INDEX_PATH):
        with open(_INDEX_PATH) as f:
            return json.load(f)
    return {}


def save_index(index):
    with open(_INDEX_PATH, "w") as f:
        json.dump(index, f, sort_keys=True, indent=2)
