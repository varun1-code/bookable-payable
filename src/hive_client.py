"""Thin wrapper around the (OpenAI-compatible) vision-LLM endpoint used for
extraction. Swappable to any OpenAI-compatible provider via env vars -
nothing else in the pipeline is provider-specific.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from openai import OpenAI

from . import config

# Pricing used only for the cost estimate this pipeline prints as it runs.
# Update if you point this at a different model/provider.
PRICE_PER_1M_INPUT = 0.50
PRICE_PER_1M_OUTPUT = 2.50


@dataclass
class UsageTotals:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    per_file: dict = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return (self.input_tokens / 1_000_000) * PRICE_PER_1M_INPUT + (
            self.output_tokens / 1_000_000
        ) * PRICE_PER_1M_OUTPUT


_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not config.HIVE_API_KEY:
            raise RuntimeError(
                "HIVE_API_KEY is not set. Put it in a .env file at the project root "
                "(see .env.example)."
            )
        _client = OpenAI(base_url=config.HIVE_BASE_URL, api_key=config.HIVE_API_KEY)
    return _client


def call_vision_json(
    system_prompt: str,
    user_text: str,
    image_data_uris: list[str],
    usage: UsageTotals,
    file_label: str,
    max_tokens: int = 4000,
    temperature: float = 0.0,
    retries: int = 2,
) -> dict:
    """Call the vision model with a system prompt, user text, and N page
    images; return the parsed JSON object from the model's reply.
    """
    client = _get_client()
    # NOTE: this endpoint silently drops the "system" role (prompt_tokens doesn't
    # even grow to include it), which was causing the model to fall back to a
    # generic hallucinated invoice instead of reading the page. Fold everything
    # into the user turn instead - confirmed via direct A/B test against this API.
    content = [{"type": "text", "text": system_prompt + "\n\n" + user_text}]
    for uri in image_data_uris:
        content.append({"type": "image_url", "image_url": {"url": uri}})

    messages = [
        {"role": "user", "content": content},
    ]

    last_err = None
    last_text = ""
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=config.HIVE_VISION_MODEL,
                messages=messages,
                # Cap retries to a smaller token budget: if this is a
                # repetition-collapse, a smaller cap fails (and can be
                # re-tried) faster and cheaper instead of burning the full
                # budget on the same loop again.
                max_tokens=max_tokens if attempt == 0 else min(max_tokens, 3000),
                # Nudge temperature up on a retry: a repetition-collapse (the
                # model looping the same phrase until it runs out of tokens)
                # tends to recur at temperature 0 on the exact same input.
                temperature=temperature if attempt == 0 else 0.5,
            )
            if resp.usage:
                usage.calls += 1
                usage.input_tokens += resp.usage.prompt_tokens or 0
                usage.output_tokens += resp.usage.completion_tokens or 0
                usage.per_file[file_label] = usage.per_file.get(file_label, 0) + (
                    resp.usage.prompt_tokens or 0
                ) + (resp.usage.completion_tokens or 0)

            last_text = resp.choices[0].message.content or ""
            if _is_degenerate(last_text):
                raise ValueError("degenerate/repetition-collapse completion")
            return _extract_json(last_text)
        except Exception as e:  # noqa: BLE001 - provider errors + parse errors both retry
            last_err = e
            if attempt == retries - 1:
                if last_text:
                    debug_dir = config.ROOT / ".llm_debug"
                    debug_dir.mkdir(exist_ok=True)
                    safe_label = re.sub(r"[^A-Za-z0-9_.-]", "_", file_label)
                    (debug_dir / f"{safe_label}.raw.txt").write_text(last_text, encoding="utf-8")
                break
            wait = min(15, 2**attempt)
            print(f"  [warn] LLM call attempt {attempt + 1} failed ({e}); retrying in {wait}s...")
            time.sleep(wait)

    raise RuntimeError(f"LLM call failed after {retries} attempts: {last_err}")


def _is_degenerate(text: str, min_repeats: int = 6, ngram_chars: int = 40) -> bool:
    """Detect a repetition-collapse completion: the same short chunk repeated
    many times in a row near the end of the output (a known small-model
    failure mode when it gets stuck, usually on hard-to-read text)."""
    if len(text) < ngram_chars * min_repeats:
        return False
    tail = text[-ngram_chars * (min_repeats + 2) :]
    chunk = tail[-ngram_chars:]
    return tail.count(chunk) >= min_repeats


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in model output: {text[:500]!r}")
    end = _find_matching_brace(text, start)
    candidate = text[start : end + 1] if end is not None else text[start:]
    try:
        # strict=False: tolerate raw control characters (literal newlines) inside
        # strings, which this model sometimes emits in multi-line addresses.
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        repaired = _repair_json(candidate)
        return json.loads(repaired, strict=False)


def _find_matching_brace(text: str, start: int) -> int | None:
    """Find the index of the '}' that closes the '{' at `start`, respecting
    string literals (so braces inside description text don't confuse depth).
    Returns None if the object never closes (truncated output)."""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _repair_json(s: str) -> str:
    """Best-effort repair for near-valid JSON. Two tiers:
    1. The output is complete content that's just missing its closing
       brackets (ran out of tokens right at the end) - simply close them,
       no content lost.
    2. The output was cut off mid-value (e.g. a repetition-collapse that
       slipped past detection) - truncate back to the last safe point and
       close brackets from there. This does lose whatever was mid-flight.
    """
    s = re.sub(r",\s*([}\]])", r"\1", s)

    depth_curly = s.count("{") - s.count("}")
    depth_square = s.count("[") - s.count("]")
    closer = "]" * max(0, depth_square) + "}" * max(0, depth_curly)
    if s.count('"') % 2 == 0 and (depth_curly > 0 or depth_square > 0):
        candidate = s + closer
        try:
            json.loads(candidate, strict=False)
            return candidate
        except json.JSONDecodeError:
            pass

    try:
        json.loads(s, strict=False)
        return s
    except json.JSONDecodeError as e:
        pos = e.pos

    # Truncate to the last position that parsed, then close any open brackets.
    truncated = s[:pos]
    last_comma = truncated.rfind(",")
    if last_comma != -1:
        truncated = truncated[:last_comma]
    depth_curly = truncated.count("{") - truncated.count("}")
    depth_square = truncated.count("[") - truncated.count("]")
    # Close an odd number of unescaped quotes (a value cut off mid-string).
    if truncated.count('"') % 2 == 1:
        truncated += '"'
    truncated += "]" * max(0, depth_square) + "}" * max(0, depth_curly)
    return truncated
