"""Validation diagnostics contain a fixed label, never model-provided text."""
import json
from copy import deepcopy

import pytest

from ai_drafts.validation import InvalidModelOutput
from media_pipeline.worker import process_one
from visual_drafts import demonstrations as demo, provider, worker
from visual_drafts.models import VisualProposal
from .test_demonstrations import answer, normalized
from .test_visual_full_video import fake_extraction, full_video, queue


@pytest.mark.parametrize(('mutation', 'field', 'reason'), [
    ('missing', 'actions', 'required'),
    ('wrong_type', 'actions', 'type'),
    ('long_caption', 'visible', 'maxLength'),
    ('extra_key', 'actions', 'additionalProperties'),
])
def test_schema_diagnostic_has_only_fixed_field_and_keyword(mutation, field, reason):
    secret = 'PRIVATE_MODEL_TEXT_NEVER_LOG_THIS'
    value = answer()
    value['actions'][0]['subject'] = secret
    if mutation == 'missing':
        value['actions'][0].pop('title')
    elif mutation == 'wrong_type':
        value['actions'] = secret
    elif mutation == 'long_caption':
        value['actions'][0]['moments'][0]['visible'] = secret * 20
    else:
        value['actions'][0][secret] = secret
    with pytest.raises(InvalidModelOutput) as caught:
        normalized(value)
    diagnostic = demo.diagnostic_for_error(caught.value)
    assert diagnostic == {'stage': 'schema', 'field': field, 'reason': reason}
    assert secret not in json.dumps(diagnostic)


def test_diagnostic_classification_rejects_unknown_and_private_values():
    secret = 'PRIVATE_MODEL_TEXT_NEVER_LOG_THIS'
    assert demo.diagnostic_for_error(InvalidModelOutput('VISUAL_EVIDENCE_ORDER_INVALID')) == {
        'stage': 'validation', 'field': 'root', 'reason': 'frame_order'}
    assert demo.diagnostic_for_error(InvalidModelOutput(secret)) == {
        'stage': 'validation', 'field': 'root', 'reason': 'other'}
    for value in ({'stage': 'schema', 'field': secret, 'reason': 'type'},
                  {'stage': 'schema', 'field': 'actions', 'reason': secret},
                  {'stage': 'schema', 'field': 'actions', 'reason': 'type', 'raw': secret}):
        assert demo.stored_diagnostic(value) is None


@pytest.mark.django_db
def test_failed_v3_window_saves_no_answer_or_secret_and_stops_calls(team, full_video, settings, monkeypatch):
    settings.VISUAL_ACTION_EVIDENCE = settings.VISUAL_SHORT_ACTION_COVERAGE = settings.VISUAL_FRAME_GROUNDING = True
    settings.VISUAL_FRAME_GROUNDING_VERSION = 'v3'
    secret = 'PRIVATE_MODEL_TEXT_NEVER_LOG_THIS'
    calls = []

    class Fake(provider.VisualProvider):
        provider, model = 'openai', 'gpt-5.4-mini'
        endpoint = 'https://api.openai.com/v1'

        def __init__(self, organization):
            pass

        def generate(self, *, frames, **kwargs):
            calls.append(1)
            value = deepcopy(answer(len(frames)))
            value['actions'][0][secret] = secret
            return value

    monkeypatch.setattr(worker, 'VisualProvider', Fake)
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    job, session = queue(team, full_video, max_ai_calls=3)
    assert process_one()
    job.refresh_from_db()
    window = session.analysis_windows.order_by('ordinal').first()
    assert job.state == 'done' and len(calls) == 1
    assert window.error_code == 'VISUAL_OUTPUT_INVALID'
    assert window.content == {'validation_diagnostic': {
        'stage': 'schema', 'field': 'actions', 'reason': 'additionalProperties'}}
    proposal = VisualProposal.objects.get()
    shown = proposal.content['analysis']['windows'][0]['validation_diagnostic']
    assert shown == window.content['validation_diagnostic']
    assert secret not in json.dumps(window.content)
    assert secret not in json.dumps(proposal.content)
