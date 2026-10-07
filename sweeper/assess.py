"""Per-thread assessment — summary, reply_needed, importance (account-first).

This replaces classify.py's model stage. It does NOT sort mail into projects or
goals: there is no registry, no operation list and no correction few-shot in
the prompt. It only answers, for each thread, "does the owner owe a human reply,
how much does it matter, and what is it about in a line" — the signals Top 5 and
reply debt need.

Account isolation: threads are batched per account, so a prompt never mixes
two mailboxes, and results are keyed by (account, thread_id) — the same Gmail
thread id in two accounts can never collide or leak a summary across accounts.

Fail-soft: the caller treats a missing key as "not assessed" (reply_needed
defaults to True, importance to 1, summary to the subject).
"""

from __future__ import annotations

import json

BATCH_SIZE = 25
SNIPPET_IN_PROMPT = 400
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = (
    "You assess email threads for a personal chief-of-staff dashboard. For each "
    "thread write a terse one-line summary (under ~14 words, concrete, no fluff) "
    "of what it is about and what, if anything, it needs, and judge:\n"
    "- reply_needed: does the OWNER owe a human response? false for "
    "auto-replies, FYIs, confirmations, receipts, completed-work notices, "
    "newsletters, and threads where the last word was the owner's to give.\n"
    "- importance: 0 = ignorable noise; 1 = routine, handle whenever; "
    "2 = a real person waiting on the owner about active work; 3 = urgent or "
    "high-stakes (deadlines, money, commitments, key relationships). Most "
    "threads are 1. Reserve 3 for things the owner would regret missing today."
)


try:
    import untrusted  # E258: email text is data, never instructions
except ImportError:  # pragma: no cover - package-relative fallback
    from . import untrusted
SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + untrusted.RULE


def _thread_for_prompt(n: int, r: dict) -> dict:
    return {
        "n": n,
        "subject": r.get("subject", ""),
        "from": r.get("last_from", ""),
        "direction": r.get("last_direction", ""),
        "participants": r.get("participants", [])[:8],
        "snippet": (r.get("snippet", "") or "")[:SNIPPET_IN_PROMPT],
    }


def build_user_prompt(batch: list) -> str:
    threads_json = untrusted.fence_json(
        [_thread_for_prompt(i, r) for i, r in enumerate(batch)], "email_threads")
    return f"""Assess each thread below.

Threads (JSON):
{threads_json}

Respond with ONLY a JSON array, one object per thread:
[{{"n": <n>, "summary": "<one line>", "reply_needed": <true|false>, "importance": <0|1|2|3>}}]"""


def _extract_json_array(text: str) -> list:
    text = text.strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON array in model response: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def normalize(row: dict) -> dict:
    """Clamp one model row to the stored shape."""
    try:
        imp = max(0, min(3, int(row.get("importance", 1))))
    except (TypeError, ValueError):
        imp = 1
    return {"summary": (row.get("summary") or "").strip(),
            "reply_needed": bool(row.get("reply_needed", True)),
            "importance": imp}


def assess_threads(records: list, env: dict, log=print, client=None,
                   model: str = DEFAULT_MODEL,
                   batch_size: int = BATCH_SIZE) -> dict:
    """{(account, thread_id): {"summary", "reply_needed", "importance"}}.

    `client` is injectable (tests pass a fake). Raises on API/auth failure so
    the orchestrator can degrade; a single unparsable batch is skipped.
    """
    if not records:
        return {}
    if client is None:
        import anthropic  # lazy so the readers stay dependency-light
        from common import require_env
        client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))

    by_account: dict[str, list] = {}
    for r in records:
        by_account.setdefault(r.get("account", ""), []).append(r)

    result: dict[tuple, dict] = {}
    tok_in = tok_out = 0
    for account, recs in by_account.items():
        for i in range(0, len(recs), batch_size):
            batch = recs[i:i + batch_size]
            resp = client.messages.create(
                model=model,
                max_tokens=2500,
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": build_user_prompt(batch)},
                    {"role": "assistant", "content": "["},  # prefill forces a JSON array
                ],
            )
            tok_in += resp.usage.input_tokens
            tok_out += resp.usage.output_tokens
            text = "[" + "".join(b.text for b in resp.content if b.type == "text")
            try:
                rows = _extract_json_array(text)
            except ValueError as e:
                log(f"assess: {account} batch {i // batch_size} parse error: {e}; skipped")
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    src = batch[int(row["n"])]
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                result[(account, src["thread_id"])] = normalize(row)
    log(f"assess: {len(result)}/{len(records)} threads via {model} | "
        f"tokens in={tok_in} out={tok_out}")
    return result
