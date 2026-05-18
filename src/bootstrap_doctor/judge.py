"""OpenClaw gateway client plus verdict cache (keep / move / unsure decisions).

For each :class:`bootstrap_doctor.heuristics.Candidate`, ask the OpenClaw
gateway whether the section should stay in the bootstrap file ("keep"),
move to a memory card ("move"), or remain undecided ("unsure"). Verdicts
are cached on disk by SHA256 of the section body so a re-run skips the
gateway entirely for unchanged content.

Design notes (full design lives in ``docs/bootstrap-doctor-design.md``):

  * The gateway is OpenAI-compatible. We POST to
    ``{cfg.gateway_url}/v1/chat/completions`` with ``temperature=0`` and
    ``response_format={"type": "json_object"}`` so the model is nudged
    toward strict JSON.
  * Cache lives at ``{cfg.cache_dir}/verdicts.json`` (override via
    ``cache_path``). Schema version is ``1``; mismatched versions are
    ignored and a fresh cache is started.
  * Per-run token budget: ``max_input_chars`` caps the total characters
    sent to the gateway across system + user prompts. Once the budget is
    blown, every remaining candidate is recorded as
    ``decision="unsure"`` with ``reasoning="budget_exceeded"`` and is
    NOT cached.
  * HTTP failures, JSON-parse failures, and schema-validation failures
    are all surfaced as a ``judge_error:`` reasoning string with
    ``decision="unsure"`` and counted in :class:`JudgeStats.failures`.
    These are also NOT cached so a retry re-asks the gateway.
  * Tests inject a stub via the ``http_post`` kwarg; production uses
    ``requests.post``. An optional ``BOOTSTRAP_DOCTOR_GATEWAY_TOKEN``
    env var becomes an ``Authorization: Bearer ...`` header.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from .heuristics import Candidate
from .parsing import Section
from .paths import Config
from .safety import atomic_write_text


# --- Constants --------------------------------------------------------------

CACHE_SCHEMA_VERSION = 1

#: The fixed taxonomy a "move" verdict may pick from. Anything outside this
#: set is silently coerced to "" so a slightly hallucinated category does
#: not bring the whole verdict down. The judge prompt also lists these so
#: drift is unlikely in practice.
CATEGORY_TAXONOMY: frozenset[str] = frozenset(
    {
        "infrastructure",
        "workflow",
        "research",
        "tools",
        "session-log",
        "deprecated",
    }
)

VALID_DECISIONS: frozenset[str] = frozenset({"keep", "move", "unsure"})

MAX_TAGS = 5

DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_MAX_INPUT_CHARS = 200_000

#: Characters that must not appear in LLM-supplied ``topic``, ``hook``,
#: or any individual ``tag``. Newlines/CR/NUL can break out of a
#: single-line breadcrumb or a YAML frontmatter scalar; a leading
#: ``#`` could pose as a markdown heading once injected.
_CONTROL_CHARS = ("\n", "\r", "\x00")


def _reject_control_chars(value: str, label: str) -> None:
    """Raise ``JudgeError`` if ``value`` contains forbidden characters."""
    for ch in _CONTROL_CHARS:
        if ch in value:
            raise JudgeError(
                f"{label} contains control characters ({ch!r})"
            )
    stripped = value.lstrip()
    if stripped.startswith("#"):
        raise JudgeError(
            f"{label} must not start with '#' (would parse as markdown heading)"
        )

#: Verbatim system prompt; see module docstring for context.
SYSTEM_PROMPT = (
    'You audit OpenClaw bootstrap files. Each bootstrap file is loaded into '
    "every session's prompt prefix and has a ~12000 char soft ceiling. You "
    'decide whether a given section should stay loaded ("keep") or move to a '
    'memory card ("move") for retrieval later. If a section is ambiguous, '
    'return "unsure".\n\n'
    "KEEP if the section is: active rules, currently-relevant state, "
    "identity, safety constraints, frequently-referenced infrastructure, or "
    "rapidly-evolving project context.\n\n"
    "MOVE if the section is: historical session logs, one-off setup notes, "
    "exemplar content, deep architectural detail that is rarely referenced, "
    "deprecated state, or anything older than 60 days that has not been "
    "updated.\n\n"
    "Respond with strict JSON only. No prose, no markdown, no code fences. "
    "Schema:\n"
    "{\n"
    '  "decision": "keep" | "move" | "unsure",\n'
    '  "topic": string (5-8 words, empty if not "move"),\n'
    '  "category": "infrastructure" | "workflow" | "research" | "tools" | '
    '"session-log" | "deprecated" | "" (empty if not "move"),\n'
    '  "tags": [string, ...] (up to 5, empty if not "move"),\n'
    '  "hook": string (one line, under 80 chars, empty if not "move"),\n'
    '  "reasoning": string (one sentence)\n'
    "}"
)


# --- Public dataclasses -----------------------------------------------------


class JudgeError(Exception):
    """Wraps HTTP errors, parse errors, and schema-validation failures."""


@dataclass(frozen=True)
class Verdict:
    """One judge decision for a single candidate."""

    section: Section
    decision: str  # "keep" | "move" | "unsure"
    topic: str
    category: str
    tags: tuple[str, ...]
    hook: str
    reasoning: str
    source: str  # "cache" or "gateway"
    body_sha: str


@dataclass
class JudgeStats:
    """Per-run accounting: request count, cache hits, failures, char budget."""

    requests_made: int = 0
    cache_hits: int = 0
    failures: int = 0
    total_input_chars: int = 0


# --- Cache helpers ----------------------------------------------------------


def _default_cache_path(cfg: Config) -> Path:
    return cfg.cache_dir / "verdicts.json"


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    """Read the cache file and return its ``entries`` dict.

    Returns an empty dict (and prints a stderr warning) on missing file,
    invalid JSON, schema-version mismatch, or any other shape issue. Never
    raises.
    """
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"warning: could not read verdict cache at {path}: {exc}",
            file=sys.stderr,
        )
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"warning: verdict cache at {path} is not valid JSON ({exc}); "
            "starting fresh",
            file=sys.stderr,
        )
        return {}
    if not isinstance(data, dict):
        print(
            f"warning: verdict cache at {path} is not a JSON object; starting fresh",
            file=sys.stderr,
        )
        return {}
    version = data.get("version")
    if version != CACHE_SCHEMA_VERSION:
        print(
            f"warning: verdict cache schema version {version!r} != "
            f"{CACHE_SCHEMA_VERSION}; ignoring old cache",
            file=sys.stderr,
        )
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    return entries


def _save_cache(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Atomically write the entire cache to ``path``."""
    payload = {"version": CACHE_SCHEMA_VERSION, "entries": entries}
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def _verdict_from_cache_entry(
    section: Section, body_sha: str, entry: dict[str, Any]
) -> Verdict:
    """Build a Verdict from a cached entry dict, with defensive defaults."""
    return Verdict(
        section=section,
        decision=str(entry.get("decision", "unsure")),
        topic=str(entry.get("topic", "")),
        category=str(entry.get("category", "")),
        tags=tuple(str(t) for t in entry.get("tags", []) or []),
        hook=str(entry.get("hook", "")),
        reasoning=str(entry.get("reasoning", "")),
        source="cache",
        body_sha=body_sha,
    )


def _cache_entry_from_verdict(verdict: Verdict) -> dict[str, Any]:
    return {
        "decision": verdict.decision,
        "topic": verdict.topic,
        "category": verdict.category,
        "tags": list(verdict.tags),
        "hook": verdict.hook,
        "reasoning": verdict.reasoning,
        "cached_at": int(time.time()),
    }


# --- Prompt builders --------------------------------------------------------


def _user_prompt(candidate: Candidate) -> str:
    section = candidate.section
    heading_path = " > ".join(section.heading_path) if section.heading_path else "(preamble)"
    reasons = ", ".join(candidate.reasons) if candidate.reasons else "(none)"
    return (
        f"File: {section.file.name}\n"
        f"Heading path: {heading_path}\n"
        f"Char count: {section.char_count}\n"
        f"Triggered heuristics: {reasons}\n\n"
        f"<section body>\n{section.body}\n</section body>"
    )


def _build_payload(candidate: Candidate, model: str) -> tuple[dict[str, Any], int]:
    """Build the OpenAI-compatible request body and report its prompt char count.

    The reported char count is system_prompt + user_prompt length, which
    is what we charge against the per-run ``max_input_chars`` budget.
    """
    user = _user_prompt(candidate)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 400,
    }
    return payload, len(SYSTEM_PROMPT) + len(user)


def _auth_headers() -> dict[str, str] | None:
    token = os.environ.get("BOOTSTRAP_DOCTOR_GATEWAY_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return None


# --- Response parsing -------------------------------------------------------


def _extract_content(body: Any) -> str:
    """Pull the assistant ``content`` string out of a chat-completion body.

    Raises :class:`JudgeError` on any shape mismatch.
    """
    if not isinstance(body, dict):
        raise JudgeError(f"response body is not a JSON object: {type(body).__name__}")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise JudgeError("response missing 'choices' array")
    first = choices[0]
    if not isinstance(first, dict):
        raise JudgeError("first choice is not a JSON object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise JudgeError("first choice missing 'message'")
    content = message.get("content")
    if not isinstance(content, str):
        raise JudgeError("message.content is not a string")
    return content


def _parse_verdict_json(content: str) -> dict[str, Any]:
    """Parse the assistant's content string as strict JSON.

    The model is asked for strict JSON; if it wraps the output in a code
    fence anyway, strip a single ```...``` or ```json...``` wrapper before
    parsing.
    """
    stripped = content.strip()
    if stripped.startswith("```"):
        # Drop opening fence (with optional language tag) and trailing fence.
        first_nl = stripped.find("\n")
        if first_nl != -1:
            stripped = stripped[first_nl + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[: -3]
        stripped = stripped.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"assistant content is not valid JSON: {exc}") from exc


def _validate_and_build_verdict(
    section: Section, body_sha: str, parsed: dict[str, Any]
) -> Verdict:
    """Validate the parsed JSON against the schema and build a Verdict.

    Raises :class:`JudgeError` on any structural problem. Unknown
    categories (for move decisions) are coerced to "" rather than treated
    as errors, so a near-miss from the model still produces a usable
    verdict.
    """
    if not isinstance(parsed, dict):
        raise JudgeError(f"verdict JSON is not an object: {type(parsed).__name__}")

    decision = parsed.get("decision")
    if decision not in VALID_DECISIONS:
        raise JudgeError(f"invalid decision: {decision!r}")

    reasoning_raw = parsed.get("reasoning", "")
    if not isinstance(reasoning_raw, str):
        raise JudgeError("reasoning is not a string")
    reasoning = reasoning_raw

    if decision == "move":
        topic_raw = parsed.get("topic", "")
        hook_raw = parsed.get("hook", "")
        if not isinstance(topic_raw, str) or not topic_raw.strip():
            raise JudgeError("move verdict missing non-empty topic")
        if not isinstance(hook_raw, str) or not hook_raw.strip():
            raise JudgeError("move verdict missing non-empty hook")
        # Reject control chars and leading '#' so a hostile model can't
        # smuggle markdown structure into the breadcrumb or YAML
        # frontmatter. Surfaces as a judge_error verdict, not cached.
        _reject_control_chars(topic_raw, "topic")
        _reject_control_chars(hook_raw, "hook")
        category_raw = parsed.get("category", "")
        if not isinstance(category_raw, str):
            raise JudgeError("category is not a string")
        category = category_raw if category_raw in CATEGORY_TAXONOMY else ""
        tags_raw = parsed.get("tags", [])
        if not isinstance(tags_raw, list):
            raise JudgeError("tags is not a list")
        tags: list[str] = []
        for t in tags_raw:
            if not isinstance(t, str):
                raise JudgeError("tags entries must be strings")
            _reject_control_chars(t, "tag")
            tags.append(t)
        tags = tags[:MAX_TAGS]
        return Verdict(
            section=section,
            decision="move",
            topic=topic_raw,
            category=category,
            tags=tuple(tags),
            hook=hook_raw,
            reasoning=reasoning,
            source="gateway",
            body_sha=body_sha,
        )

    # keep / unsure: move-only fields are forced empty regardless of what
    # the model returned.
    return Verdict(
        section=section,
        decision=decision,
        topic="",
        category="",
        tags=(),
        hook="",
        reasoning=reasoning,
        source="gateway",
        body_sha=body_sha,
    )


def _error_verdict(section: Section, body_sha: str, err: Exception) -> Verdict:
    """Build the canonical 'judge_error:' verdict for any failure path."""
    return Verdict(
        section=section,
        decision="unsure",
        topic="",
        category="",
        tags=(),
        hook="",
        reasoning=f"judge_error: {err}",
        source="gateway",
        body_sha=body_sha,
    )


def _budget_verdict(section: Section, body_sha: str) -> Verdict:
    return Verdict(
        section=section,
        decision="unsure",
        topic="",
        category="",
        tags=(),
        hook="",
        reasoning="budget_exceeded",
        source="gateway",
        body_sha=body_sha,
    )


# --- HTTP helpers -----------------------------------------------------------


def _default_http_post(url: str, payload: dict[str, Any]) -> Any:
    """Real HTTP path: ``requests.post`` with our default timeout + auth."""
    headers = _auth_headers()
    return requests.post(
        url, json=payload, timeout=DEFAULT_REQUEST_TIMEOUT, headers=headers
    )


def _call_gateway(
    candidate: Candidate,
    cfg: Config,
    http_post: Callable[[str, dict[str, Any]], Any],
) -> tuple[Verdict, int]:
    """One round-trip: build payload, call ``http_post``, parse the response.

    Returns ``(verdict, prompt_chars)``. On any failure the verdict is the
    canonical ``judge_error:`` shape and the caller bumps ``stats.failures``.
    """
    section = candidate.section
    body_sha = _sha256(section.body)
    payload, prompt_chars = _build_payload(candidate, cfg.gateway_model)
    url = cfg.gateway_url.rstrip("/") + "/v1/chat/completions"
    try:
        response = http_post(url, payload)
    except Exception as exc:  # network failure, DNS, refused conn, etc.
        return _error_verdict(section, body_sha, exc), prompt_chars

    status = getattr(response, "status_code", None)
    if status != 200:
        text = ""
        try:
            text = getattr(response, "text", "") or ""
        except Exception:
            text = ""
        if text:
            print(
                f"gateway error {status}: {text[:500]}",
                file=sys.stderr,
            )
        return (
            _error_verdict(
                section, body_sha, JudgeError(f"HTTP {status}")
            ),
            prompt_chars,
        )

    try:
        body = response.json()
        content = _extract_content(body)
        parsed = _parse_verdict_json(content)
        verdict = _validate_and_build_verdict(section, body_sha, parsed)
    except JudgeError as exc:
        return _error_verdict(section, body_sha, exc), prompt_chars
    except Exception as exc:  # response.json() can raise ValueError etc.
        return (
            _error_verdict(
                section, body_sha, JudgeError(f"response parse: {exc}")
            ),
            prompt_chars,
        )

    return verdict, prompt_chars


# --- Misc helpers -----------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_uncacheable(verdict: Verdict) -> bool:
    """A verdict is NOT cached if it's a judge_error or a budget_exceeded.

    Both states are transient: the gateway might be flaky, or a future
    run might have headroom. Caching either would lock a bad outcome in.
    """
    if verdict.decision != "unsure":
        return False
    reasoning = verdict.reasoning or ""
    return reasoning.startswith("judge_error:") or reasoning == "budget_exceeded"


# --- Public entrypoint ------------------------------------------------------


def judge_all(
    candidates: list[Candidate],
    cfg: Config,
    *,
    cache_path: Path | None = None,
    use_cache: bool = True,
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    http_post: Callable[[str, dict[str, Any]], Any] | None = None,
) -> tuple[list[Verdict], JudgeStats]:
    """Judge every candidate, returning verdicts in input order plus stats.

    See module docstring for caching, budget, and failure semantics.
    """
    stats = JudgeStats()
    if not candidates:
        return [], stats

    resolved_cache_path = cache_path if cache_path is not None else _default_cache_path(cfg)
    post: Callable[[str, dict[str, Any]], Any] = (
        http_post if http_post is not None else _default_http_post
    )

    cache_entries: dict[str, dict[str, Any]] = (
        _load_cache(resolved_cache_path) if use_cache else {}
    )
    new_entries_written = False
    budget_blown = False

    verdicts: list[Verdict] = []
    for candidate in candidates:
        section = candidate.section
        body_sha = _sha256(section.body)

        if budget_blown:
            verdicts.append(_budget_verdict(section, body_sha))
            continue

        # Cache hit short-circuit (only when use_cache is True).
        if use_cache and body_sha in cache_entries:
            verdict = _verdict_from_cache_entry(
                section, body_sha, cache_entries[body_sha]
            )
            verdicts.append(verdict)
            stats.cache_hits += 1
            continue

        # Budget pre-check: rough char count for this candidate. Strategy
        # is "simple bail": if this one would push us over, mark it and
        # every later candidate as budget_exceeded.
        _, prompt_chars = _build_payload(candidate, cfg.gateway_model)
        if stats.total_input_chars + prompt_chars > max_input_chars:
            budget_blown = True
            verdicts.append(_budget_verdict(section, body_sha))
            continue

        verdict, used_chars = _call_gateway(candidate, cfg, post)
        stats.total_input_chars += used_chars

        if verdict.reasoning.startswith("judge_error:"):
            stats.failures += 1
            verdicts.append(verdict)
            # Do not cache; allow retry on next run.
            continue
        stats.requests_made += 1

        verdicts.append(verdict)
        if use_cache and not _is_uncacheable(verdict):
            cache_entries[body_sha] = _cache_entry_from_verdict(verdict)
            new_entries_written = True

    if use_cache and new_entries_written:
        _save_cache(resolved_cache_path, cache_entries)

    return verdicts, stats
