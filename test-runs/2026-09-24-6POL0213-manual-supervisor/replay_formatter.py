"""One OpenRouter replay of a formatter prompt to capture finish_reason + exact usage.

Usage:
  replay_formatter.py resolve                 -> print what prod's escalation would pick for sonnet/opus
  replay_formatter.py run <prompt_dir> [model] -> POST system.md+user.md, save response + telemetry

Key comes from `get-secret.sh OPENROUTER_API_KEY` (never hard-coded, never printed).
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = "/Users/mriechers/Developer/cardigan/.claude/worktrees/linear-dreaming-bumblebee"
sys.path.insert(0, REPO)
RUN = Path(__file__).resolve().parent

key = subprocess.run(["get-secret.sh", "OPENROUTER_API_KEY"], capture_output=True, text=True, check=True).stdout.strip()
os.environ["OPENROUTER_API_KEY"] = key

from api.services import model_roster  # noqa: E402
from api.services.completeness import count_content_words  # noqa: E402

MAX_TOKENS = 16384  # what PR #407 sets on backends.openrouter


async def resolve():
    for fam in ("sonnet", "opus", "haiku"):
        print(fam, "->", await model_roster.newest_in_family(fam, ["fast", "fable"]))


def run(prompt_dir: Path, model: str):
    import httpx

    system = (prompt_dir / "system.md").read_text(encoding="utf-8")
    user = (prompt_dir / "user.md").read_text(encoding="utf-8")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": MAX_TOKENS,
        "usage": {"include": True},
    }
    if os.environ.get("NO_REASONING"):
        payload["reasoning"] = {"enabled": False}
        print("reasoning disabled in payload")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://pbswisconsin.org",
        "X-Title": "Cardigan",
    }
    t0 = time.time()
    r = httpx.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers, timeout=600)
    dt = time.time() - t0
    out_dir = prompt_dir / "replay"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%H%M%S")
    (out_dir / f"raw_{stamp}.json").write_text(r.text, encoding="utf-8")
    print("HTTP", r.status_code, "raw saved to", out_dir / f"raw_{stamp}.json")
    r.raise_for_status()
    data = r.json()
    choice = data["choices"][0]
    content = choice["message"].get("content")
    usage = data.get("usage", {})
    if content is None:
        print("CONTENT IS NONE. choice keys:", list(choice.keys()), "message keys:", list(choice["message"].keys()))
        print("finish_reason:", choice.get("finish_reason"), "native:", choice.get("native_finish_reason"), "error:", data.get("error"), choice.get("error"))
        print("usage:", usage)
        return
    words = count_content_words(content)
    reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
    tel = {
        "reasoning_tokens": reasoning_tokens,
        "content_tokens": (usage.get("completion_tokens") or 0) - (reasoning_tokens or 0),
        "content_tokens_per_content_word": round(((usage.get("completion_tokens") or 0) - (reasoning_tokens or 0)) / words, 3) if words else None,
        "model_requested": model,
        "model_actual": data.get("model"),
        "provider": data.get("provider"),
        "finish_reason": choice.get("finish_reason"),
        "native_finish_reason": choice.get("native_finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cost_usd": usage.get("cost"),
        "elapsed_s": round(dt, 1),
        "max_tokens_sent": MAX_TOKENS,
        "output_content_words": words,
        "tokens_per_content_word": round(usage.get("completion_tokens", 0) / words, 3) if words else None,
        "raw_output_words": len(content.split()),
        "tokens_per_raw_word": round(usage.get("completion_tokens", 0) / len(content.split()), 3) if content.split() else None,
    }
    (out_dir / "output.md").write_text(content, encoding="utf-8")
    (out_dir / "telemetry.json").write_text(json.dumps(tel, indent=2))
    print(json.dumps(tel, indent=2))


if __name__ == "__main__":
    if sys.argv[1] == "resolve":
        asyncio.run(resolve())
    else:
        run(Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "anthropic/claude-sonnet-4.6")
