"""Tests for stricter grant review criteria: first-timer hour cap, content
gate, and the evidence-first rubric in the reviewer prompts."""

import asyncio
import json

import pytest

from src.features.grants import assessor


# ---------------------------------------------------------------------------
# First-timer hour cap ("start small")
# ---------------------------------------------------------------------------

def _approved(hours):
    return {
        'reasoning': 'r',
        'decision': 'approved',
        'response': 'approved',
        'gpu_type': 'H100_80GB',
        'recommended_hours': hours,
    }


def test_first_timer_cap_rejects_over_cap_hours():
    err = assessor._validate(_approved(30), first_grant=True)
    assert err is not None
    assert 'capped at' in err and 'needs_review' in err


def test_first_timer_cap_accepts_cap_hours():
    assert assessor._validate(_approved(10), first_grant=True) is None


def test_repeat_grantor_not_capped():
    assert assessor._validate(_approved(30), first_grant=False) is None


def test_first_timer_cap_env_tunable(monkeypatch):
    monkeypatch.setenv('GRANTS_FIRST_TIMER_MAX_HOURS', '15')
    assert assessor.first_timer_max_hours() == 15
    assert assessor._validate(_approved(12), first_grant=True) is None
    assert assessor._validate(_approved(16), first_grant=True) is not None
    monkeypatch.delenv('GRANTS_FIRST_TIMER_MAX_HOURS')


def test_first_timer_cap_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv('GRANTS_FIRST_TIMER_MAX_HOURS', 'not-a-number')
    assert assessor.first_timer_max_hours() == 10
    monkeypatch.delenv('GRANTS_FIRST_TIMER_MAX_HOURS')


# ---------------------------------------------------------------------------
# Content gate — never adjudicate a title-only / empty application
# ---------------------------------------------------------------------------

def test_content_gate_rejects_empty_body():
    ok, reason = assessor.is_application_content_sufficient('Some Project Title', '')
    assert not ok
    assert 'screenshots' in reason or 'paste' in reason


def test_content_gate_rejects_title_only_reply():
    # Applicant pasted a screenshot; message body is empty (the MageFlow case)
    ok, _ = assessor.is_application_content_sufficient('MageTrail: Proof-of-concept', '\n')
    assert not ok


def test_content_gate_accepts_substantive_body():
    body = (
        "I want to finetune Microsoft's MageFlow 4B model for illustration "
        "style transfer. The GPU hours will train a LoRA on a 200-image dataset. "
        "Repo: https://github.com/example/magetrail"
    )
    ok, _ = assessor.is_application_content_sufficient('MageTrail', body)
    assert ok


def test_content_gate_boundary():
    minimum = assessor.min_application_chars()
    ok, _ = assessor.is_application_content_sufficient('T', 'x' * minimum)
    assert ok
    ok, _ = assessor.is_application_content_sufficient('T', 'x' * (minimum - 1))
    assert not ok


# ---------------------------------------------------------------------------
# Stricter rubric present in the prompts
# ---------------------------------------------------------------------------

def test_system_prompt_has_evidence_over_assertion_rule():
    assert 'Evidence over assertion' in assessor.SYSTEM_PROMPT
    assert 'never infer project content from the thread title' in assessor.SYSTEM_PROMPT
    assert 'Self-reported results without a linked artifact cannot support an approval' in assessor.SYSTEM_PROMPT


def test_system_prompt_has_first_timer_start_small_rule():
    assert 'capped at 10' in assessor.SYSTEM_PROMPT
    assert 'start small' in assessor.SYSTEM_PROMPT.lower()
    assert 'needs_review' in assessor.SYSTEM_PROMPT


def test_system_prompt_has_engagement_scrutiny_rule():
    assert 'RAISES scrutiny' in assessor.SYSTEM_PROMPT
    assert 'Message count is not demonstrated community standing' in assessor.SYSTEM_PROMPT


def test_system_prompt_has_manual_review_triggers():
    assert '## Manual Review Triggers (needs_review)' in assessor.SYSTEM_PROMPT


def test_admin_review_prompt_honours_first_timer_cap():
    assert 'capped at 10 hours' in assessor.ADMIN_REVIEW_PROMPT


# ---------------------------------------------------------------------------
# End-to-end: the LLM loop honours the cap via validation feedback
# ---------------------------------------------------------------------------

def test_assessment_loop_clamps_first_timer_hours(monkeypatch):
    calls = []

    async def fake_llm(client, model, **kwargs):
        messages = kwargs['messages']
        # attempt 1: fresh prompt -> 30h; after feedback -> 10h
        if len(messages) == 1:
            resp = {'reasoning': 'r', 'decision': 'approved', 'response': 'ok',
                    'gpu_type': 'H100_80GB', 'recommended_hours': 30}
        else:
            resp = {'reasoning': 'r', 'decision': 'approved', 'response': 'ok',
                    'gpu_type': 'H100_80GB', 'recommended_hours': 10}
        calls.append(len(messages))
        return json.dumps(resp)

    monkeypatch.setattr(assessor, 'get_llm_response', fake_llm)

    result = asyncio.run(assessor.assess_application(
        '**App**\n\nI want to finetune a model for illustration. '
        'Repo: https://github.com/example/x',
        grant_history=[],
        engagement=None,
    ))
    assert result['recommended_hours'] == 10
    assert len(calls) == 2  # first attempt rejected by the cap, second accepted


def test_assessment_loop_allows_repeat_grantor_hours(monkeypatch):
    async def fake_llm(client, model, **kwargs):
        return json.dumps({'reasoning': 'r', 'decision': 'approved', 'response': 'ok',
                           'gpu_type': 'H100_80GB', 'recommended_hours': 30})

    monkeypatch.setattr(assessor, 'get_llm_response', fake_llm)
    result = asyncio.run(assessor.assess_application(
        '**App**\n\nI want to finetune a model. Repo: https://github.com/example/x',
        grant_history=[{'status': 'paid', 'created_at': '2026-01-01', 'gpu_type': 'H100_80GB'}],
        engagement=None,
    ))
    assert result['recommended_hours'] == 30
