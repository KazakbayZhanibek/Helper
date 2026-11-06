from copy import deepcopy
from types import SimpleNamespace

import pytest
from django.utils import timezone

from ai_budget.models import AIUsageReservation
from ai_drafts.validation import InvalidModelOutput
from visual_drafts import demonstrations as demo, provider, services, planning, worker
from visual_drafts.models import VisualProposal
from visual_drafts.teaching import present_instruction
from media_pipeline.worker import process_one
from .test_action_protocol import images, instance
from .test_frame_grounding import frames
from .test_frame_grounding_v2 import session
from .test_visual_full_video import full_video, queue, fake_extraction
from .test_grounding_pipeline import saved_grounded_proposal
from .conftest import post, save, document, checked_review, CONFIRMATIONS


def selected():
    result = session()
    result.analysis_budget['frame_grounding'] = 'v3'
    return result


def answer(count=3):
    return {'schema_version': demo.WIRE_VERSION, 'language': 'ru', 'gaps': [], 'actions': [{
        'subject': 'Заготовка', 'title': 'Опускаем заготовку на подставку',
        'moments': [{'image_index': 1, 'visible': 'Заготовка в руке над подставкой.'},
                    {'image_index': count, 'visible': 'Заготовка нижним краем касается подставки.'}],
        'change': 'Рука опустила заготовку до подставки.', 'kind': 'action',
        'instruction': 'Берём заготовку рукой. Опускаем её нижний край на подставку.',
    }]}


def normalized(value=None, count=3):
    source = frames(images(count))
    return demo.normalize_grounded_actions(value or answer(count), source, selected()), source


def test_method_and_selected_photo_captions_survive_exact_read_projection():
    content, source = normalized()
    checked = services.checked_instruction_content(content, source, selected())
    assert checked == content
    shown = present_instruction(checked)['steps'][0]
    assert shown['description'] == answer()['actions'][0]['instruction']
    assert shown['photo_demonstration'] == [
        {'frame_id': str(source[0].id), 'caption': answer()['actions'][0]['moments'][0]['visible']},
        {'frame_id': str(source[-1].id), 'caption': answer()['actions'][0]['moments'][-1]['visible']}]
    assert len(shown['evidence_frame_ids']) == 3  # Interior evidence is not lost.


@pytest.mark.parametrize('kind', ['support', 'unclear'])
def test_pose_and_unclear_method_do_not_become_commands_or_hide_supported_sibling(kind):
    value = answer()
    sibling = deepcopy(value['actions'][0]);sibling['kind'] = kind
    value['actions'].append(sibling)
    content, source = normalized(value)
    assert len(content['steps']) == 1
    assert demo.reconcile_grounding(content, source, selected()) == content


@pytest.mark.parametrize('mutation', ['unknown', 'reverse', 'duplicate', 'single', 'blank'])
def test_invalid_or_invented_photo_references_are_rejected(mutation):
    value=answer();moments=value['actions'][0]['moments']
    if mutation == 'unknown': moments[-1]['image_index']=12
    elif mutation == 'reverse': moments.reverse()
    elif mutation == 'duplicate': moments[-1]['image_index']=1
    elif mutation == 'single': moments.pop()
    else: moments[0]['visible']=' '
    with pytest.raises(InvalidModelOutput): normalized(value)


@pytest.mark.parametrize('mutation', ['method', 'photo', 'gap', 'step', 'pairs'])
def test_modified_saved_explanation_or_photo_is_not_applied(mutation):
    content, source=normalized()
    if mutation=='method': content['observations'][0]['movement']='Выдуманное действие.'
    elif mutation=='photo': content['observations'][0]['before_frame_id']=str(source[-1].id)
    elif mutation=='gap': content['gaps'].append({'id':'invented'})
    elif mutation=='step': content['steps'][0]['description']='Выдуманное действие.'
    else: content['temporal_coverage']['pairs'].pop()
    with pytest.raises(InvalidModelOutput): demo.reconcile_grounding(content, source, selected())


def test_cut_does_not_turn_into_an_action_and_is_reconciled():
    value=answer();value['gaps']=[{'from_image_index':1,'to_image_index':2,'reason':'cut','question':'Какая операция осталась за монтажом?'}]
    content, source=normalized(value)
    assert not content['steps']
    assert demo.reconcile_grounding(content, source, selected())==content


def test_one_request_with_existing_image_and_output_limits_and_no_refinement():
    source=images(12);model=instance('openai');model.short_action_coverage='v1';model.frame_grounding='v3'
    _,schema,request,body=model.prepare_request(language='ru',mode='upload',frames=source)
    assert schema['properties']['schema_version']['enum']==[demo.WIRE_VERSION]
    assert len([i for i in body['messages'][1]['content'] if i['type']=='image_url'])==12
    assert body['max_completion_tokens']==8192
    assert 'temporal_pairs' not in request
    content,_=normalized()
    assert not planning.needs_refinement(content)
    assert worker.protocol_for_job(SimpleNamespace(payload=selected().analysis_budget),selected())=='3'


@pytest.mark.django_db
def test_multiwindow_worker_budget_read_and_apply_projection(team, full_video, settings, monkeypatch):
    settings.VISUAL_ACTION_EVIDENCE=settings.VISUAL_SHORT_ACTION_COVERAGE=settings.VISUAL_FRAME_GROUNDING=True
    settings.VISUAL_FRAME_GROUNDING_VERSION='v3'
    calls=[]
    class Fake(provider.VisualProvider):
        provider,model='openai','gpt-5.4-mini'
        endpoint='https://api.openai.com/v1'
        def __init__(self,organization): pass
        def generate(self,*,frames,**kwargs):
            calls.append(len(frames));return answer(len(frames))
    monkeypatch.setattr(worker,'VisualProvider',Fake)
    monkeypatch.setattr(worker,'uploaded_frames',fake_extraction)
    job,live=queue(team,full_video,max_ai_calls=3)
    assert process_one()
    job.refresh_from_db();assert job.state=='done',(job.error_code,job.error_summary)
    assert len(calls)==AIUsageReservation.objects.count()==3
    assert not live.analysis_windows.filter(parent__isnull=False).exists()
    item=VisualProposal.objects.get();shown=services.proposal_data(item)
    assert shown['can_apply'] and len(shown['steps'])==3
    assert all(s['photo_demonstration'] for s in shown['steps'])
    assert not full_video.working.steps.exists()
    from core.services import etag
    applied=post(team.clients['author'],full_video.url+f'/visual-proposals/{item.id}/apply',
                 {'confirm_replace':True},etag=etag(full_video.working),key='demo-apply')
    assert applied.status_code==200,applied.json()
    working=applied.json()
    assert [s['photo_demonstration'] for s in working['steps']]==[s['photo_demonstration'] for s in shown['steps']]
    edited=save(team.clients['author'],full_video.url,working,document(working,title='Понятная инструкция с фотографиями'))
    assert edited.status_code==200,edited.json()
    assert [s['photo_demonstration'] for s in edited.json()['steps']]==[s['photo_demonstration'] for s in shown['steps']]
    draft=SimpleNamespace(url=full_video.url,working=edited.json())
    reviewed=checked_review(team,draft)
    published=post(team.clients['reviewer'],draft.url+'/publish',{'confirmations':CONFIRMATIONS},etag=reviewed['etag'],key='demo-publish')
    assert published.status_code==201,published.json()
    assert [s['photo_demonstration'] for s in published.json()['snapshot']['steps']]==[s['photo_demonstration'] for s in shown['steps']]


@pytest.mark.django_db
def test_archived_results_disappear_without_deleting_evidence_or_budget(team,draft):
    item,live,_=saved_grounded_proposal(team,draft)
    count=live.frames.count();receipts=AIUsageReservation.objects.count()
    item.archived_at=timezone.now();item.save(update_fields=['archived_at'])
    response=team.clients['author'].get(draft.url+'/visual-proposals')
    assert response.status_code==200 and response.json()['items']==[]
    assert team.clients['author'].get(draft.url+'/visual-proposals/'+str(item.id)).status_code==404
    assert live.frames.count()==count and AIUsageReservation.objects.count()==receipts
    assert services.session_data(live)['observations']==[]


@pytest.mark.django_db
def test_archive_command_hides_finished_jobs_and_preserves_queued_jobs(team,draft):
    from django.core.management import call_command
    from media_pipeline.models import ProcessingJob
    from core.models import Instruction
    item,live,_=saved_grounded_proposal(team,draft)
    instruction=Instruction.objects.get(pk=draft.working['instruction_id'])
    done=ProcessingJob.objects.create(organization=team.org,instruction=instruction,asset=instruction.source_asset,requested_by=team.users['author'],kind='analyze_video',state='done',stage='done',payload={},result={})
    queued=ProcessingJob.objects.create(organization=team.org,instruction=instruction,asset=instruction.source_asset,requested_by=team.users['author'],kind='analyze_video',state='queued',stage='queued',payload={},result={})
    call_command('archive_ai_results',organization=str(team.org.id),execute=False)
    item.refresh_from_db();assert item.archived_at is None
    call_command('archive_ai_results',organization=str(team.org.id),execute=True)
    done.refresh_from_db();queued.refresh_from_db();item.refresh_from_db()
    assert done.archived_at and item.archived_at and queued.archived_at is None
    response=team.clients['author'].get(draft.url+'/jobs')
    assert response.status_code==200
    assert [j['id'] for j in response.json()['items']]==[str(queued.id)]
