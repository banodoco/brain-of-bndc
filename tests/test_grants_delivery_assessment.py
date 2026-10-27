"""Delivery updates are evidence bounded and independent from funding decisions."""

import asyncio
import json

from src.features.grants import delivery_assessor


GRANT = {'thread_content': 'Funded scope: train and publish an open-source image LoRA and training script.',
         'status': 'paid', 'applicant_id': 42}


def _answer(classification, refs=None, response='Thanks for the update.'):
    return json.dumps({'classification': classification, 'summary': 'Compared update with funded scope.',
                       'evidence_refs': refs or [], 'response': response})


def test_thanks_is_unrelated_without_llm_or_ack(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('ordinary conversation must not call the LLM')
    monkeypatch.setattr(delivery_assessor, 'get_llm_response', forbidden)
    result = asyncio.run(delivery_assessor.assess_delivery(GRANT, {'content': 'Thanks!'}, []))
    assert result['classification'] == 'unrelated'
    assert result['response'] == ''


def test_attachment_only_update_requests_details_instead_of_silent_ignore(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('metadata alone has a deterministic needs_info result')
    monkeypatch.setattr(delivery_assessor, 'get_llm_response', forbidden)
    evidence = [{'id': 'attachment:1', 'status': 'unverified_reference',
                 'reference_type': 'source_attachment',
                 'attachments': [{'filename': 'demo.mp4', 'inspected': False}]}]
    result = asyncio.run(delivery_assessor.assess_delivery(GRANT, {'content': ''}, evidence))
    assert result['classification'] == 'needs_info'
    assert result['response']


def test_thanks_with_attachment_is_not_unrelated(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('uninspected attachment needs explanation')
    monkeypatch.setattr(delivery_assessor, 'get_llm_response', forbidden)
    result = asyncio.run(delivery_assessor.assess_delivery(
        GRANT, {'content': 'Thanks!', 'attachments': [{'filename': 'weights.safetensors'}]}, []))
    assert result['classification'] == 'needs_info'


def test_progress_can_be_reported_without_completion(monkeypatch):
    async def fake(*args, **kwargs):
        assert 'funded_grant' in kwargs['messages'][0]['content']
        assert 'train and publish' in kwargs['messages'][0]['content']
        return _answer('progress')
    monkeypatch.setattr(delivery_assessor, 'get_llm_response', fake)
    result = asyncio.run(delivery_assessor.assess_delivery(
        GRANT, {'content': 'Training is running; I have 2 of 10 epochs complete.'}, []))
    assert result['classification'] == 'progress'


def test_completion_claim_with_unavailable_link_never_completes(monkeypatch):
    async def fake(*args, **kwargs):
        return _answer('completed', ['https://github.com/example/model'])
    monkeypatch.setattr(delivery_assessor, 'get_llm_response', fake)
    evidence = [{'id': 'ext:1', 'url': 'https://github.com/example/model',
                 'status': 'unverified_reference', 'reference_type': 'external', 'content': None}]
    result = asyncio.run(delivery_assessor.assess_delivery(
        GRANT, {'content': 'It is done, see repo https://github.com/example/model'}, evidence))
    assert result['classification'] == 'needs_review'
    assert result['evidence_refs'] == []


def test_fetched_discord_self_claim_and_metadata_do_not_prove_completion(monkeypatch):
    async def fake(*args, **kwargs):
        return _answer('completed', ['discord:1:2:3'])
    monkeypatch.setattr(delivery_assessor, 'get_llm_response', fake)
    evidence = [{'id': 'discord:1:2:3', 'url': 'https://discord.com/channels/1/2/3',
                 'status': 'available', 'reference_type': 'discord_message',
                 'content': 'I trained the model and it is all done.',
                 'attachments': [{'filename': 'model.safetensors', 'inspected': False}]}]
    result = asyncio.run(delivery_assessor.assess_delivery(GRANT, {'content': 'Done!'}, evidence))
    assert result['classification'] == 'needs_info'


def test_inspected_artifact_can_support_completion(monkeypatch):
    async def fake(*args, **kwargs):
        return _answer('completed', ['artifact:1'])
    monkeypatch.setattr(delivery_assessor, 'get_llm_response', fake)
    evidence = [{'id': 'artifact:1', 'url': 'https://example.org/artifact',
                 'status': 'available', 'reference_type': 'inspected_artifact',
                 'content': 'Repository contains the open-source image LoRA weights, training script, config, and usage example.'}]
    result = asyncio.run(delivery_assessor.assess_delivery(GRANT, {'content': 'Published the weights and script.'}, evidence))
    assert result['classification'] == 'completed'
    assert result['evidence_refs'] == ['artifact:1']


def test_unknown_available_evidence_type_cannot_support_completion(monkeypatch):
    async def fake(*args, **kwargs):
        return _answer('completed', ['artifact:1'])
    monkeypatch.setattr(delivery_assessor, 'get_llm_response', fake)
    evidence = [{'id': 'artifact:1', 'status': 'available',
                 'content': 'Model is done according to author.'}]
    result = asyncio.run(delivery_assessor.assess_delivery(GRANT, {'content': 'Done.'}, evidence))
    assert result['classification'] == 'needs_info'


def test_missing_original_scope_prevents_completion(monkeypatch):
    async def fake(*args, **kwargs):
        return _answer('completed', ['artifact:1'])
    monkeypatch.setattr(delivery_assessor, 'get_llm_response', fake)
    evidence = [{'id': 'artifact:1', 'status': 'available', 'reference_type': 'external',
                 'content': 'Some published model weights'}]
    result = asyncio.run(delivery_assessor.assess_delivery({'status': 'paid'}, {'content': 'Finished.'}, evidence))
    assert result['classification'] == 'needs_info'


def test_malformed_output_retries_then_needs_review(monkeypatch):
    calls = []
    async def fake(*args, **kwargs):
        calls.append(1)
        return 'not json'
    monkeypatch.setattr(delivery_assessor, 'get_llm_response', fake)
    result = asyncio.run(delivery_assessor.assess_delivery(GRANT, {'content': 'I shipped an update.'}, []))
    assert len(calls) == 3
    assert result['classification'] == 'needs_review'


def test_fabricated_citation_is_rejected(monkeypatch):
    async def fake(*args, **kwargs):
        return _answer('progress', ['https://invented.invalid'])
    monkeypatch.setattr(delivery_assessor, 'get_llm_response', fake)
    result = asyncio.run(delivery_assessor.assess_delivery(GRANT, {'content': 'Ran two epochs.'}, []))
    assert result['classification'] == 'needs_review'
    assert result['evidence_refs'] == []
