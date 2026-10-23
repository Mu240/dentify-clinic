"""Tiny keyword retrieval over faq.json. Replace faq.json with the client's real facts."""
import json
import os
import re

_PATH = os.path.join(os.path.dirname(__file__), "data", "faq.json")
with open(_PATH, encoding="utf-8") as f:
    FAQ = json.load(f)


def _has(low, kw):
    return re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", low) is not None


def search(text):
    """Return {id, answer, score} for the best match, or None."""
    low = text.lower()
    best = None
    for item in FAQ:
        req = item.get("require")
        if req and not any(_has(low, k) for k in req):
            continue
        score = 1 if req else 0
        for k in item.get("keywords", []):
            if _has(low, k):
                score += 2 if " " in k else 1
        if score and (best is None or score > best["score"]):
            best = {"id": item["id"], "answer": item["answer"], "score": score}
    return best


def all_facts():
    return "\n".join(f"- {i['answer']}" for i in FAQ)
