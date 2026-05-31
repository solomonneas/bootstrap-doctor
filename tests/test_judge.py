"""Tests for judge.py: gateway client + verdict cache + token-budget cap."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from bootstrap_doctor.heuristics import Candidate
from bootstrap_doctor.judge import (
    JudgeStats,
    Verdict,
    judge_all,
)
from bootstrap_doctor.parsing import Section
from bootstrap_doctor.paths import Config, resolve_config

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def make_section(
    file: str | Path = "AGENTS.md",
    heading: str = "Setup",
    body: str = "some body content here",
    level: int = 2,
    heading_path: tuple[str, ...] | None = None,
) -> Section:
    """Build a Section without going through the parser."""
    if isinstance(file, str):
        file = Path(file)
    if heading_path is None:
        heading_path = (heading,) if level > 0 else ()
    return Section(
        file=file,
        heading_level=level,
        heading_text=heading,
        heading_path=heading_path,
        body=body,
        char_count=len(body),
        line_count=body.count("\n") + 1 if body else 0,
        start_line=1,
        end_line=1 + body.count("\n"),
    )


def make_candidate(
    section: Section | None = None,
    reasons: tuple[str, ...] = ("large",),
) -> Candidate:
    if section is None:
        section = make_section()
    return Candidate(section=section, reasons=reasons)


@pytest.fixture
def cfg(tmp_path: Path, workspace_dir: Path, cards_dir: Path) -> Config:
    """A fully-formed Config rooted at the tmp_path workspace.

    Uses an isolated cache_dir under tmp_path so tests never touch the
    real ~/.cache/bootstrap-doctor. Pre-creates the cache dir so tests
    that seed the cache file by hand can do so directly; the dedicated
    ``test_judge_creates_cache_dir_lazily_on_write`` test asserts the
    lazy-create behavior of ``resolve_config`` itself.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'''
workspace_dir = "{workspace_dir}"
cards_dir = "{cards_dir}"
gateway_url = "http://localhost:18789"
gateway_model = "test-model"

[cache]
dir = "{cache_dir}"
'''
    )
    return resolve_config(config_file=str(config_file))


class FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code: int, json_body: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or (json.dumps(json_body) if json_body is not None else "")

    def json(self) -> Any:
        if self._json_body is None:
            raise ValueError("no json body set")
        return self._json_body


def _chat_completion(content: str) -> dict[str, Any]:
    """Wrap a `content` string in the OpenAI chat-completion envelope."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _ok_move() -> dict[str, Any]:
    return _chat_completion(
        json.dumps(
            {
                "decision": "move",
                "topic": "old setup notes",
                "category": "session-log",
                "tags": ["setup", "old"],
                "hook": "Notes about an old one-time setup step.",
                "reasoning": "Historical session log; safe to promote.",
            }
        )
    )


def _ok_keep() -> dict[str, Any]:
    return _chat_completion(
        json.dumps(
            {
                "decision": "keep",
                "topic": "",
                "category": "",
                "tags": [],
                "hook": "",
                "reasoning": "Active rule, must stay loaded.",
            }
        )
    )


def _ok_unsure() -> dict[str, Any]:
    return _chat_completion(
        json.dumps(
            {
                "decision": "unsure",
                "topic": "",
                "category": "",
                "tags": [],
                "hook": "",
                "reasoning": "Could go either way.",
            }
        )
    )


# ---------------------------------------------------------------------------
# Verdict / dataclass shape
# ---------------------------------------------------------------------------


def test_verdict_is_frozen_and_carries_fields() -> None:
    sec = make_section()
    v = Verdict(
        section=sec,
        decision="move",
        topic="a topic",
        category="session-log",
        tags=("a", "b"),
        hook="a hook",
        reasoning="because",
        source="gateway",
        body_sha=hashlib.sha256(sec.body.encode("utf-8")).hexdigest(),
    )
    with pytest.raises(Exception):
        # frozen dataclass; reassignment should fail
        v.decision = "keep"  # type: ignore[misc]


def test_judge_stats_defaults_zero() -> None:
    s = JudgeStats()
    assert s.requests_made == 0
    assert s.cache_hits == 0
    assert s.failures == 0
    assert s.total_input_chars == 0


# ---------------------------------------------------------------------------
# HTTP path: valid responses
# ---------------------------------------------------------------------------


def test_move_response_populates_verdict(cfg: Config) -> None:
    sec = make_section(body="A historical session log of yesterday's work.")
    cand = make_candidate(sec)

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        calls.append((url, payload))
        return FakeResponse(200, _ok_move())

    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)

    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.decision == "move"
    assert v.topic == "old setup notes"
    assert v.category == "session-log"
    assert v.tags == ("setup", "old")
    assert v.hook.startswith("Notes about")
    assert v.reasoning.startswith("Historical session log")
    assert v.source == "gateway"
    assert v.body_sha == hashlib.sha256(sec.body.encode("utf-8")).hexdigest()
    assert stats.requests_made == 1
    assert stats.cache_hits == 0
    assert stats.failures == 0
    assert stats.total_input_chars > 0

    # URL must be gateway_url + /v1/chat/completions
    assert len(calls) == 1
    assert calls[0][0] == cfg.gateway_url.rstrip("/") + "/v1/chat/completions"
    # Payload structure: model, messages, response_format, temperature.
    payload = calls[0][1]
    assert payload["model"] == cfg.gateway_model
    assert payload["temperature"] == 0.0
    assert payload["response_format"] == {"type": "json_object"}
    assert isinstance(payload["messages"], list)
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"
    # User prompt mentions the section heading and reasons.
    assert "Setup" in payload["messages"][1]["content"]
    assert "large" in payload["messages"][1]["content"]
    assert sec.body in payload["messages"][1]["content"]


def test_keep_response_yields_empty_move_fields(cfg: Config) -> None:
    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_keep())

    verdicts, _ = judge_all([cand], cfg, http_post=fake_post)
    v = verdicts[0]
    assert v.decision == "keep"
    assert v.topic == ""
    assert v.category == ""
    assert v.tags == ()
    assert v.hook == ""
    assert v.reasoning.startswith("Active rule")


def test_unsure_response_recorded(cfg: Config) -> None:
    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_unsure())

    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].decision == "unsure"
    assert verdicts[0].source == "gateway"
    assert stats.failures == 0


# ---------------------------------------------------------------------------
# HTTP path: failure modes
# ---------------------------------------------------------------------------


def test_http_500_handled_as_judge_error(cfg: Config) -> None:
    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(500, text="internal server error")

    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)
    v = verdicts[0]
    assert v.decision == "unsure"
    assert v.reasoning.startswith("judge_error:")
    assert v.source == "gateway"
    assert stats.failures == 1
    # requests_made counts successful gateway calls only; failures are separate.
    assert stats.requests_made == 0


def test_malformed_json_handled_as_judge_error(cfg: Config) -> None:
    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        # Returns 200 but with content that isn't JSON.
        return FakeResponse(200, _chat_completion("not actually json at all"))

    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)
    v = verdicts[0]
    assert v.decision == "unsure"
    assert v.reasoning.startswith("judge_error:")
    assert stats.failures == 1


def test_invalid_decision_handled_as_judge_error(cfg: Config) -> None:
    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(
            200,
            _chat_completion(
                json.dumps(
                    {
                        "decision": "yes",
                        "topic": "",
                        "category": "",
                        "tags": [],
                        "hook": "",
                        "reasoning": "n/a",
                    }
                )
            ),
        )

    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)
    v = verdicts[0]
    assert v.decision == "unsure"
    assert v.reasoning.startswith("judge_error:")
    assert stats.failures == 1


def test_failure_is_not_cached(cfg: Config) -> None:
    cand = make_candidate()
    calls = {"n": 0}

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        calls["n"] += 1
        return FakeResponse(500, text="boom")

    judge_all([cand], cfg, http_post=fake_post)
    judge_all([cand], cfg, http_post=fake_post)
    assert calls["n"] == 2  # both runs hit the gateway, neither cached


# ---------------------------------------------------------------------------
# Cache path
# ---------------------------------------------------------------------------


def test_cache_miss_writes_file(cfg: Config) -> None:
    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_move())

    cache_file = cfg.cache_dir / "verdicts.json"
    assert not cache_file.exists()
    judge_all([cand], cfg, http_post=fake_post)
    assert cache_file.exists()
    data = json.loads(cache_file.read_text())
    assert data["version"] == 1
    body_sha = hashlib.sha256(cand.section.body.encode("utf-8")).hexdigest()
    assert body_sha in data["entries"]
    entry = data["entries"][body_sha]
    assert entry["decision"] == "move"
    assert entry["topic"] == "old setup notes"
    assert "cached_at" in entry


def test_cache_hit_skips_gateway(cfg: Config) -> None:
    cand = make_candidate()
    calls = {"n": 0}

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        calls["n"] += 1
        return FakeResponse(200, _ok_move())

    # First run populates cache.
    judge_all([cand], cfg, http_post=fake_post)
    assert calls["n"] == 1

    # Second run hits cache only.
    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)
    assert calls["n"] == 1
    assert verdicts[0].source == "cache"
    assert verdicts[0].decision == "move"
    assert stats.cache_hits == 1
    assert stats.requests_made == 0


def test_corrupt_cache_starts_fresh(
    cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cache_file = cfg.cache_dir / "verdicts.json"
    cache_file.write_text("{not valid json")

    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_move())

    verdicts, _ = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].decision == "move"
    assert verdicts[0].source == "gateway"
    err = capsys.readouterr().err
    assert "warn" in err.lower() or "warning" in err.lower()


def test_version_mismatch_starts_fresh(cfg: Config) -> None:
    cache_file = cfg.cache_dir / "verdicts.json"
    body_sha = hashlib.sha256(b"some body content here").hexdigest()
    cache_file.write_text(
        json.dumps(
            {
                "version": 999,
                "entries": {
                    body_sha: {
                        "decision": "move",
                        "topic": "stale schema",
                        "category": "deprecated",
                        "tags": [],
                        "hook": "stale",
                        "reasoning": "n/a",
                        "cached_at": 1,
                    }
                },
            }
        )
    )

    cand = make_candidate()
    called = {"n": 0}

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        called["n"] += 1
        return FakeResponse(200, _ok_keep())

    verdicts, _ = judge_all([cand], cfg, http_post=fake_post)
    # Old schema should be ignored; gateway re-asked.
    assert called["n"] == 1
    assert verdicts[0].decision == "keep"


def test_use_cache_false_bypasses_cache(cfg: Config) -> None:
    cand = make_candidate()
    calls = {"n": 0}

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        calls["n"] += 1
        return FakeResponse(200, _ok_move())

    judge_all([cand], cfg, use_cache=False, http_post=fake_post)
    judge_all([cand], cfg, use_cache=False, http_post=fake_post)
    assert calls["n"] == 2
    cache_file = cfg.cache_dir / "verdicts.json"
    assert not cache_file.exists()


def test_missing_cache_file_is_fine(cfg: Config) -> None:
    """No cache file on disk is not an error and produces no warnings."""
    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_keep())

    verdicts, _ = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].decision == "keep"


def test_explicit_cache_path_used(cfg: Config, tmp_path: Path) -> None:
    custom = tmp_path / "custom-cache.json"
    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_move())

    judge_all([cand], cfg, cache_path=custom, http_post=fake_post)
    assert custom.exists()
    # Default location should NOT be written when explicit path is given.
    default = cfg.cache_dir / "verdicts.json"
    assert not default.exists()


# ---------------------------------------------------------------------------
# Token-budget cap
# ---------------------------------------------------------------------------


def test_budget_exceeded_bails_remaining_candidates(cfg: Config) -> None:
    # Size the bodies so the first candidate fits but the second pushes past.
    # System prompt is ~1700 chars; one user prompt with these bodies is
    # roughly 600-800 chars. Two requests would exceed a 4000-char cap.
    c1 = make_candidate(make_section(heading="One", body="x" * 500))
    c2 = make_candidate(make_section(heading="Two", body="y" * 500))

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_keep())

    # Budget tuned to fit exactly one request, not two.
    verdicts, stats = judge_all(
        [c1, c2], cfg, max_input_chars=2700, http_post=fake_post
    )
    assert len(verdicts) == 2
    # First one is asked; second one bails on budget.
    decisions = [v.decision for v in verdicts]
    assert decisions[0] == "keep"
    assert decisions[1] == "unsure"
    assert verdicts[1].reasoning == "budget_exceeded"
    assert verdicts[1].source == "gateway"


def test_budget_exceeded_not_cached(cfg: Config) -> None:
    c1 = make_candidate(make_section(heading="One", body="x" * 500))
    c2 = make_candidate(make_section(heading="Two", body="y" * 500))

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_keep())

    judge_all([c1, c2], cfg, max_input_chars=2700, http_post=fake_post)
    cache_file = cfg.cache_dir / "verdicts.json"
    if cache_file.exists():
        data = json.loads(cache_file.read_text())
        body_sha_2 = hashlib.sha256(c2.section.body.encode("utf-8")).hexdigest()
        assert body_sha_2 not in data["entries"]


# ---------------------------------------------------------------------------
# JudgeStats accounting
# ---------------------------------------------------------------------------


def test_stats_sums_to_total(cfg: Config) -> None:
    c1 = make_candidate(make_section(body="alpha body content here"))
    c2 = make_candidate(make_section(heading="Two", body="beta body content here"))
    c3 = make_candidate(make_section(heading="Three", body="gamma body content here"))

    responses = iter([_ok_move(), _ok_keep(), None])

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        r = next(responses)
        if r is None:
            return FakeResponse(500, text="oops")
        return FakeResponse(200, r)

    verdicts, stats = judge_all([c1, c2, c3], cfg, http_post=fake_post)
    assert len(verdicts) == 3
    assert stats.requests_made + stats.cache_hits + stats.failures == 3
    # Two succeeded, one failed.
    assert stats.requests_made == 2
    assert stats.failures == 1
    assert stats.cache_hits == 0


def test_stats_total_input_chars_grows(cfg: Config) -> None:
    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_keep())

    _, stats = judge_all([cand], cfg, http_post=fake_post)
    assert stats.total_input_chars > 0


# ---------------------------------------------------------------------------
# Schema coercion
# ---------------------------------------------------------------------------


def test_tags_truncated_to_five(cfg: Config) -> None:
    cand = make_candidate()

    payload_body = json.dumps(
        {
            "decision": "move",
            "topic": "topic here",
            "category": "tools",
            "tags": ["a", "b", "c", "d", "e", "f", "g"],
            "hook": "hook here",
            "reasoning": "fine",
        }
    )

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _chat_completion(payload_body))

    verdicts, _ = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].tags == ("a", "b", "c", "d", "e")


def test_unknown_category_coerced_to_empty(cfg: Config) -> None:
    cand = make_candidate()

    payload_body = json.dumps(
        {
            "decision": "move",
            "topic": "topic here",
            "category": "wildcard-cat-not-in-taxonomy",
            "tags": ["x"],
            "hook": "hook here",
            "reasoning": "fine",
        }
    )

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _chat_completion(payload_body))

    verdicts, _ = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].decision == "move"
    assert verdicts[0].category == ""


def test_keep_with_unknown_category_stays_empty(cfg: Config) -> None:
    """For non-move decisions, category is irrelevant and should be ''."""
    cand = make_candidate()

    payload_body = json.dumps(
        {
            "decision": "keep",
            "topic": "ignored",
            "category": "wildcard",
            "tags": ["ignored"],
            "hook": "ignored",
            "reasoning": "keep it",
        }
    )

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _chat_completion(payload_body))

    verdicts, _ = judge_all([cand], cfg, http_post=fake_post)
    v = verdicts[0]
    assert v.decision == "keep"
    assert v.category == ""
    assert v.topic == ""
    assert v.tags == ()
    assert v.hook == ""


def test_move_with_newline_in_topic_treated_as_error(cfg: Config) -> None:
    """Newlines in topic could inject content into card YAML frontmatter
    or the markdown breadcrumb. Reject at the judge boundary."""
    cand = make_candidate()
    payload_body = json.dumps(
        {
            "decision": "move",
            "topic": "legit topic\n## Injected H2",
            "category": "tools",
            "tags": ["a"],
            "hook": "a hook",
            "reasoning": "fine",
        }
    )

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _chat_completion(payload_body))

    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].decision == "unsure"
    assert verdicts[0].reasoning.startswith("judge_error:")
    assert "control" in verdicts[0].reasoning.lower() or "newline" in verdicts[0].reasoning.lower()
    assert stats.failures == 1
    # Must not be cached.
    cache_file = cfg.cache_dir / "verdicts.json"
    if cache_file.exists():
        data = json.loads(cache_file.read_text())
        assert data.get("entries", {}) == {}


def test_move_with_newline_in_hook_treated_as_error(cfg: Config) -> None:
    cand = make_candidate()
    payload_body = json.dumps(
        {
            "decision": "move",
            "topic": "ok topic",
            "category": "tools",
            "tags": [],
            "hook": "line1\n## Injected H2",
            "reasoning": "fine",
        }
    )

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _chat_completion(payload_body))

    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].decision == "unsure"
    assert verdicts[0].reasoning.startswith("judge_error:")


def test_move_with_leading_hash_in_topic_treated_as_error(cfg: Config) -> None:
    """Topic starting with '#' could pose as a markdown heading after
    injection into the breadcrumb line context."""
    cand = make_candidate()
    payload_body = json.dumps(
        {
            "decision": "move",
            "topic": "# sneaky heading",
            "category": "tools",
            "tags": [],
            "hook": "a hook",
            "reasoning": "fine",
        }
    )

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _chat_completion(payload_body))

    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].decision == "unsure"
    assert verdicts[0].reasoning.startswith("judge_error:")


def test_move_with_carriage_return_in_tag_treated_as_error(cfg: Config) -> None:
    cand = make_candidate()
    payload_body = json.dumps(
        {
            "decision": "move",
            "topic": "ok",
            "category": "tools",
            "tags": ["clean", "bad\rtag"],
            "hook": "hook",
            "reasoning": "fine",
        }
    )

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _chat_completion(payload_body))

    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].decision == "unsure"
    assert verdicts[0].reasoning.startswith("judge_error:")


def test_move_with_null_byte_in_topic_treated_as_error(cfg: Config) -> None:
    cand = make_candidate()
    payload_body = json.dumps(
        {
            "decision": "move",
            "topic": "topic\x00with-null",
            "category": "tools",
            "tags": [],
            "hook": "hook",
            "reasoning": "fine",
        }
    )

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _chat_completion(payload_body))

    verdicts, _ = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].decision == "unsure"
    assert verdicts[0].reasoning.startswith("judge_error:")


def test_move_with_missing_topic_treated_as_error(cfg: Config) -> None:
    cand = make_candidate()

    payload_body = json.dumps(
        {
            "decision": "move",
            "topic": "",
            "category": "tools",
            "tags": ["a"],
            "hook": "a hook",
            "reasoning": "missing topic",
        }
    )

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _chat_completion(payload_body))

    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].decision == "unsure"
    assert verdicts[0].reasoning.startswith("judge_error:")
    assert stats.failures == 1


# ---------------------------------------------------------------------------
# Default http_post (no injection) -- verify the wiring without hitting net
# ---------------------------------------------------------------------------


def test_default_http_post_uses_requests_post(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When http_post is None, judge_all falls back to requests.post."""
    cand = make_candidate()
    captured: dict[str, Any] = {}

    def fake_requests_post(url: str, *, json: Any, timeout: int, headers: Any = None) -> FakeResponse:
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout
        captured["headers"] = headers
        return FakeResponse(200, _ok_keep())

    import bootstrap_doctor.judge as judge_module

    monkeypatch.setattr(judge_module.requests, "post", fake_requests_post)

    verdicts, _ = judge_all([cand], cfg)
    assert verdicts[0].decision == "keep"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["timeout"] > 0


def test_auth_header_when_token_env_set(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_GATEWAY_TOKEN", "super-secret")
    cand = make_candidate()
    captured: dict[str, Any] = {}

    def fake_requests_post(url: str, *, json: Any, timeout: int, headers: Any = None) -> FakeResponse:
        captured["headers"] = headers
        return FakeResponse(200, _ok_keep())

    import bootstrap_doctor.judge as judge_module

    monkeypatch.setattr(judge_module.requests, "post", fake_requests_post)

    judge_all([cand], cfg)
    assert captured["headers"] is not None
    assert captured["headers"].get("Authorization") == "Bearer super-secret"


# ---------------------------------------------------------------------------
# Empty candidate list
# ---------------------------------------------------------------------------


def test_empty_candidate_list(cfg: Config) -> None:
    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        raise AssertionError("should not be called")

    verdicts, stats = judge_all([], cfg, http_post=fake_post)
    assert verdicts == []
    assert stats.requests_made == 0
    assert stats.cache_hits == 0
    assert stats.failures == 0


# ---------------------------------------------------------------------------
# Cache value sanity
# ---------------------------------------------------------------------------


def test_judge_creates_cache_dir_lazily_on_write(
    workspace_dir: Path, cards_dir: Path, tmp_path: Path
) -> None:
    """Cache dir is only created when judge_all needs to write to it.

    Resolving a Config alone must not touch ~/.cache.
    """
    cache_dir = tmp_path / "lazy-cache"
    assert not cache_dir.exists()
    cfg_file = tmp_path / "cfg.toml"
    cfg_file.write_text(
        f'''
workspace_dir = "{workspace_dir}"
cards_dir = "{cards_dir}"
gateway_url = "http://localhost:18789"
gateway_model = "test-model"

[cache]
dir = "{cache_dir}"
'''
    )
    cfg = resolve_config(config_file=str(cfg_file))
    # Config resolution must NOT have created the dir.
    assert not cache_dir.exists()
    assert cfg.cache_dir == cache_dir.resolve()

    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_move())

    judge_all([cand], cfg, http_post=fake_post)
    # Now the cache file (and its parent) should exist.
    assert (cache_dir / "verdicts.json").exists()


def test_corrupt_cache_entry_invalid_decision_falls_back_to_gateway(
    cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cache entry whose decision is outside VALID_DECISIONS must
    be treated as a miss, not blindly returned as a Verdict."""
    cache_file = cfg.cache_dir / "verdicts.json"
    cand = make_candidate()
    body_sha = hashlib.sha256(cand.section.body.encode("utf-8")).hexdigest()
    cache_file.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    body_sha: {
                        "decision": "destroy",  # not in VALID_DECISIONS
                        "topic": "stale",
                        "category": "deprecated",
                        "tags": [],
                        "hook": "stale",
                        "reasoning": "n/a",
                        "cached_at": 1,
                    }
                },
            }
        )
    )
    calls = {"n": 0}

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        calls["n"] += 1
        return FakeResponse(200, _ok_keep())

    verdicts, stats = judge_all([cand], cfg, http_post=fake_post)
    # Gateway was re-asked because the cached entry was invalid.
    assert calls["n"] == 1
    assert verdicts[0].decision == "keep"
    assert verdicts[0].source == "gateway"
    err = capsys.readouterr().err.lower()
    assert "cache" in err and ("invalid" in err or "warn" in err)


def test_corrupt_cache_entry_non_dict_falls_back_to_gateway(
    cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-dict cache entry (e.g., null) must not crash judge_all."""
    cache_file = cfg.cache_dir / "verdicts.json"
    cand = make_candidate()
    body_sha = hashlib.sha256(cand.section.body.encode("utf-8")).hexdigest()
    cache_file.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    body_sha: None,
                },
            }
        )
    )

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_keep())

    verdicts, _ = judge_all([cand], cfg, http_post=fake_post)
    assert verdicts[0].decision == "keep"
    assert verdicts[0].source == "gateway"


def test_corrupt_cache_entry_move_missing_topic_falls_back(
    cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 'move' cache entry with empty topic must be re-judged, not
    returned as a malformed move verdict (which would crash trim)."""
    cache_file = cfg.cache_dir / "verdicts.json"
    cand = make_candidate()
    body_sha = hashlib.sha256(cand.section.body.encode("utf-8")).hexdigest()
    cache_file.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    body_sha: {
                        "decision": "move",
                        "topic": "",  # invalid: move requires non-empty topic
                        "category": "tools",
                        "tags": [],
                        "hook": "h",
                        "reasoning": "stale",
                        "cached_at": 1,
                    }
                },
            }
        )
    )
    calls = {"n": 0}

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        calls["n"] += 1
        return FakeResponse(200, _ok_keep())

    verdicts, _ = judge_all([cand], cfg, http_post=fake_post)
    assert calls["n"] == 1
    assert verdicts[0].source == "gateway"


def test_cached_at_is_recent_unix_ts(cfg: Config) -> None:
    cand = make_candidate()

    def fake_post(url: str, payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, _ok_move())

    before = int(time.time())
    judge_all([cand], cfg, http_post=fake_post)
    after = int(time.time())

    cache_file = cfg.cache_dir / "verdicts.json"
    data = json.loads(cache_file.read_text())
    body_sha = hashlib.sha256(cand.section.body.encode("utf-8")).hexdigest()
    cached_at = data["entries"][body_sha]["cached_at"]
    assert before <= cached_at <= after
