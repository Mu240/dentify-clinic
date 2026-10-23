"""Optional LLM polish for open-ended questions. Booking logic never goes through the LLM.

LLM_BACKEND=none     (default) rule-based answers only, works fully offline
LLM_BACKEND=ollama   local & free  -> `ollama pull llama3.2:3b`   (OLLAMA_URL, LLM_MODEL)
LLM_BACKEND=groq     free API tier -> set GROQ_API_KEY            (LLM_MODEL, LLM_BASE_URL)
Any failure returns None and the agent falls back to the rule-based answer."""
import json
import os
import urllib.request

SYSTEM = ("You are the friendly phone receptionist of a dental practice. Answer in one or two short "
          "spoken-style sentences using ONLY the facts below. If the facts don't cover it, say a team "
          "member will follow up and offer to take a message. Never give medical diagnoses.\nFACTS:\n{facts}")


def enabled():
    return os.environ.get("LLM_BACKEND", "none").lower() in ("ollama", "groq", "openai")


def _post(url, payload, headers, timeout=12):
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def answer(question, facts):
    backend = os.environ.get("LLM_BACKEND", "none").lower()
    msgs = [{"role": "system", "content": SYSTEM.format(facts=facts)},
            {"role": "user", "content": question}]
    try:
        if backend == "ollama":
            url = os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/chat"
            out = _post(url, {"model": os.environ.get("LLM_MODEL", "llama3.2:3b"),
                              "messages": msgs, "stream": False}, {"Content-Type": "application/json"})
            return out["message"]["content"].strip()
        if backend in ("groq", "openai"):
            key = os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY", "")
            url = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1") + "/chat/completions"
            out = _post(url, {"model": os.environ.get("LLM_MODEL", "llama-3.1-8b-instant"),
                              "messages": msgs, "max_tokens": 120, "temperature": 0.3},
                        {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            return out["choices"][0]["message"]["content"].strip()
    except Exception:
        return None
    return None
