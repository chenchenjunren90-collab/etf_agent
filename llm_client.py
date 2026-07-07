"""DeepSeek OpenAI-compatible client with disk cache, retry, and token budget.

Used by ``llm_decider`` for the daily prediction and the backtest replay.

Design points:
- DeepSeek speaks the OpenAI Chat Completions wire format, so we hand-roll the
  HTTP call with the standard library; that keeps the dependency surface tiny.
- Every response is cached on disk under ``data/llm_cache/{date_tag}/{hash}.json``.
  The same prompt on the same trade date never hits the network twice, which is
  what makes backtests deterministic and cheap.
- Token usage is summed across the process and capped by
  ``LLM_DAILY_TOKEN_BUDGET`` to avoid a runaway bill.
- Caller passes a tiny ``schema`` dict; we enforce required keys and basic
  types.  Any failure raises ``LLMResponseError`` so callers can decide whether
  to fall back to the rule-based path.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from http.client import IncompleteRead
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "data" / "llm_cache"

DEFAULT_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DAILY_TOKEN_BUDGET = int(os.environ.get("LLM_DAILY_TOKEN_BUDGET", "500000"))
REQUEST_TIMEOUT = int(os.environ.get("LLM_REQUEST_TIMEOUT", "60"))


class LLMUnavailable(RuntimeError):
    """Raised when the LLM cannot be called at all (no key, budget hit, network)."""


class LLMBudgetExceeded(LLMUnavailable):
    """Daily token budget has been reached for this process."""


class LLMResponseError(RuntimeError):
    """LLM responded but the JSON was unparseable or failed schema validation."""


def get_api_key() -> str | None:
    return os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY")


def is_available() -> bool:
    return bool(get_api_key())


_STATE: dict[str, int] = {"tokens_used": 0, "calls": 0, "cache_hits": 0}


def stats() -> dict[str, int]:
    return dict(_STATE)


def reset_stats() -> None:
    _STATE.update({"tokens_used": 0, "calls": 0, "cache_hits": 0})


def _safe_tag(date_tag: str) -> str:
    return date_tag.replace("/", "-").replace("\\", "-").strip()


def _cache_path(date_tag: str, prompt_hash: str) -> Path:
    return CACHE_DIR / _safe_tag(date_tag) / f"{prompt_hash}.json"


def _prompt_hash(model: str, system: str, prompt: str, temperature: float) -> str:
    payload = json.dumps(
        {"model": model, "system": system, "prompt": prompt, "temperature": temperature},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _read_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _validate_schema(data: Any, schema: dict[str, Any] | None) -> tuple[bool, str]:
    if not schema:
        return True, "ok"
    if not isinstance(data, dict):
        return False, "response is not a JSON object"
    for key in schema.get("required", []):
        if key not in data:
            return False, f"missing required key: {key}"
    for key, type_or_tuple in (schema.get("types") or {}).items():
        if key in data and not isinstance(data[key], type_or_tuple):
            return False, f"key {key} expected {type_or_tuple} got {type(data[key])}"
    return True, "ok"


def _post_json(url: str, body: dict[str, Any], *, api_key: str) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    return json.loads(text)


def _ensure_json_hint(system_text: str, prompt: str) -> tuple[str, str]:
    """DeepSeek json_object mode requires the word 'json' somewhere in messages."""
    if "json" not in (system_text + prompt).lower():
        prompt = prompt.rstrip() + "\n\n请严格以 JSON 对象格式回复（json）。"
    return system_text, prompt


def _strip_code_fence(text: str) -> str:
    s = text.strip()
    if not s.startswith("```"):
        return s
    s = s.lstrip("`")
    if s.lower().startswith("json"):
        s = s[4:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def call_json(
    prompt: str,
    *,
    schema: dict[str, Any] | None = None,
    system: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    date_tag: str | None = None,
    use_cache: bool = True,
    cache_only: bool = False,
    retries: int = 2,
) -> dict[str, Any]:
    """Call DeepSeek with JSON mode and an on-disk cache.

    Returns a dict with ``data`` (parsed JSON), ``cache_hit``, ``model``,
    ``usage`` and ``prompt_hash``.
    """
    system_text = system or "你是 A 股日内 ETF 决策助手，严格按要求输出 JSON。"
    system_text, prompt = _ensure_json_hint(system_text, prompt)
    tag = _safe_tag(date_tag or datetime.now().strftime("%Y-%m-%d"))
    ph = _prompt_hash(model, system_text, prompt, temperature)
    cache_file = _cache_path(tag, ph)

    if use_cache:
        cached = _read_cache(cache_file)
        if cached and isinstance(cached.get("data"), dict):
            ok, _ = _validate_schema(cached["data"], schema)
            if ok:
                _STATE["cache_hits"] += 1
                cached["cache_hit"] = True
                return cached

    if cache_only:
        raise LLMUnavailable(f"cache_only=True but no cache hit for {tag}/{ph}")

    api_key = get_api_key()
    if not api_key:
        raise LLMUnavailable("DEEPSEEK_API_KEY not set")

    if _STATE["tokens_used"] >= DAILY_TOKEN_BUDGET:
        raise LLMBudgetExceeded(
            f"daily token budget exceeded ({_STATE['tokens_used']}/{DAILY_TOKEN_BUDGET})"
        )

    url = f"{DEFAULT_BASE_URL.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }

    raw: dict[str, Any] | None = None
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = _post_json(url, body, api_key=api_key)
            break
        except urllib.error.HTTPError as exc:
            last_err = exc
            retriable = exc.code in (408, 409, 425, 429, 500, 502, 503, 504)
            if retriable and attempt < retries:
                time.sleep(min(8.0, 1.5 ** (attempt + 1)))
                continue
            try:
                body_text = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                body_text = ""
            raise LLMUnavailable(f"DeepSeek HTTP {exc.code}: {body_text}")
        except (urllib.error.URLError, TimeoutError, OSError, IncompleteRead) as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(min(8.0, 1.5 ** (attempt + 1)))
                continue
            raise LLMUnavailable(f"DeepSeek network: {exc}")

    if raw is None:
        raise LLMUnavailable(f"unreachable: {last_err}")

    try:
        msg = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMResponseError(f"unexpected response shape: {exc}")

    try:
        parsed = json.loads(msg)
    except json.JSONDecodeError as exc:
        try:
            parsed = json.loads(_strip_code_fence(msg))
        except Exception:
            raise LLMResponseError(
                f"non-JSON response (first 200 chars): {msg[:200]}"
            ) from exc

    ok, err = _validate_schema(parsed, schema)
    if not ok:
        raise LLMResponseError(f"schema check failed: {err}")

    usage = raw.get("usage") or {}
    _STATE["calls"] += 1
    _STATE["tokens_used"] += int(usage.get("total_tokens", 0) or 0)

    payload = {
        "data": parsed,
        "cache_hit": False,
        "model": model,
        "usage": usage,
        "prompt_hash": ph,
        "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if use_cache:
        try:
            _write_cache(cache_file, payload)
        except Exception as exc:
            print(f"[llm_client] failed to write cache: {exc}")
    return payload


__all__ = [
    "call_json",
    "is_available",
    "get_api_key",
    "stats",
    "reset_stats",
    "LLMUnavailable",
    "LLMBudgetExceeded",
    "LLMResponseError",
    "DEFAULT_MODEL",
    "CACHE_DIR",
]
