import hashlib
import json
import uuid

from django.db import transaction
from django.utils import timezone

from core import services as s
from core.models import WorkingCopy, WorkingStep
from media_pipeline.jobs import enqueue_job, job_data
from .models import DraftProposal, Transcript
from .providers import asr_configuration, capabilities
from .validation import InvalidModelOutput, validate_proposal


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def editable_working(instruction, supplied):
    working = WorkingCopy.objects.select_for_update().get(instruction=instruction)
    s.require_match(working, supplied)
    if not working.is_open or working.workflow != 'draft':
        s.fail('INVALID_STATE', 'Верните инструкцию в черновик перед работой с ИИ.', 409)
    return working


def ready_asset(instruction):
    asset = instruction.source_asset
    if not asset or asset.status != 'ready':
        s.fail('MEDIA_NOT_READY', 'Сначала загрузите и подготовьте видео.', 409)
    return asset


def latest_transcript(asset):
    return Transcript.objects.filter(asset=asset, organization_id=asset.organization_id).order_by('-revision').first()


def transcript_data(transcript):
    return {'id': str(transcript.id), 'asset_id': str(transcript.asset_id), 'revision': transcript.revision, 'language': transcript.language, 'provider': transcript.provider, 'model': transcript.model, 'duration_ms': transcript.duration_ms, 'created_at': s.utc(transcript.created_at), 'warnings': transcript.warnings, 'segments': [{'id': str(segment.id), 'ordinal': segment.ordinal, 'start_ms': segment.start_ms, 'end_ms': segment.end_ms, 'raw_text': segment.raw_text, 'text': segment.text} for segment in transcript.segments.all()]}


def transcript_for_asset(member, asset_id):
    instruction = member.organization.instruction_set.filter(source_asset_id=asset_id, lifecycle='active').select_related('source_asset').first()
    if not instruction:
        s.fail('NOT_FOUND', 'Расшифровка не найдена.', 404)
    result = latest_transcript(instruction.source_asset)
    if not result:
        s.fail('NOT_FOUND', 'Для этого видео ещё нет расшифровки.', 404)
    return result


def proposal_data(proposal, instruction=None):
    instruction = instruction or proposal.instruction
    current = latest_transcript(instruction.source_asset) if instruction.source_asset_id else None
    stale = instruction.working_copy.revision != proposal.base_working_revision or not current or current.id != proposal.transcript_id
    return {'id': str(proposal.id), 'instruction_id': str(proposal.instruction_id), 'transcript_id': str(proposal.transcript_id), 'transcript_revision': proposal.transcript.revision, 'base_working_revision': proposal.base_working_revision, 'provider': proposal.provider, 'model': proposal.model, 'created_at': s.utc(proposal.created_at), 'applied_at': s.utc(proposal.applied_at), 'stale': bool(stale), **proposal.content}


@transaction.atomic
def request_transcription(member, instruction_id, data, supplied):
    s.allowed_fields(data, set())
    instruction = s.instruction_for(member, instruction_id, lock=True)
    working = editable_working(instruction, supplied)
    asset = ready_asset(instruction)
    if not getattr(asset, 'has_audio', False) or not getattr(asset, 'audio_key', ''):
        s.fail('NO_AUDIO', 'В видео нет доступной звуковой дорожки. Добавьте шаги вручную.', 422)
    config = asr_configuration()
    if not config['available']:
        s.fail('ASR_UNAVAILABLE', config['reason'], 503)
    payload = {'language': working.language, 'base_working_revision': working.revision}
    digest = fingerprint({'asset_id': str(asset.id), 'language': working.language, 'provider': config['provider'], 'model': config['model'], 'config': 'asr-v1'})
    job = enqueue_job(member, instruction, 'transcribe', payload, digest)
    s.audit(member, 'transcription.requested', job)
    return job_data(job), 202


@transaction.atomic
def request_proposal(member, instruction_id, data, supplied):
    s.allowed_fields(data, {'transcript_id', 'transcript_revision', 'base_working_revision'})
    instruction = s.instruction_for(member, instruction_id, lock=True)
    working = editable_working(instruction, supplied)
    asset = ready_asset(instruction)
    if type(data.get('base_working_revision')) is not int or type(data.get('transcript_revision')) is not int:
        s.fail('VALIDATION_ERROR', 'Укажите целочисленные версии рабочей копии и расшифровки.')
    try:
        transcript_id = uuid.UUID(str(data.get('transcript_id', '')))
    except ValueError:
        s.fail('VALIDATION_ERROR', 'Укажите корректную расшифровку.')
    transcript = Transcript.objects.filter(id=transcript_id, asset=asset, organization=member.organization).first()
    if not transcript:
        s.fail('NOT_FOUND', 'Расшифровка не найдена.', 404)
    newest = latest_transcript(asset)
    if data['base_working_revision'] != working.revision or data['transcript_revision'] != transcript.revision or newest.id != transcript.id:
        s.fail('STALE_PROPOSAL', 'Исходные данные изменились. Загрузите актуальную версию.', 409)
    segments = list(transcript.segments.all())
    if not segments or len(segments) > 300 or sum(len(segment.text) for segment in segments) > 18_000:
        s.fail('TRANSCRIPT_SIZE_LIMIT', 'Для ИИ нужна расшифровка из 1–300 фрагментов, не более 18 000 символов.', 422)
    config = capabilities(member.organization)['step_drafts']
    if not config['available']:
        s.fail('AI_DISABLED', config['reason'], 503)
    payload = {'transcript_id': str(transcript.id), 'transcript_revision': transcript.revision, 'base_working_revision': working.revision, 'language': working.language}
    digest = fingerprint({**payload, 'provider': config['provider'], 'model': config['model'], 'config': 'steps-v1'})
    job = enqueue_job(member, instruction, 'draft_steps', payload, digest)
    s.audit(member, 'draft_proposal.requested', job)
    return job_data(job), 202


def proposal_for(member, instruction_id, proposal_id, lock=False):
    instruction = s.instruction_for(member, instruction_id, lock=lock)
    query = DraftProposal.objects.select_related('transcript').filter(instruction=instruction, organization=member.organization, transcript__organization=member.organization, transcript__asset_id=instruction.source_asset_id)
    if lock:
        query = query.select_for_update(of=('self',))
    proposal = query.filter(id=proposal_id).first()
    if not proposal:
        s.fail('NOT_FOUND', 'Предложение не найдено.', 404)
    return instruction, proposal


@transaction.atomic
def apply_proposal(member, instruction_id, proposal_id, data, supplied):
    s.allowed_fields(data, {'confirm_replace'})
    if data.get('confirm_replace') is not True:
        s.fail('REPLACEMENT_CONFIRMATION_REQUIRED', 'Подтвердите замену текущих шагов предложением ИИ.')
    instruction, proposal = proposal_for(member, instruction_id, proposal_id, lock=True)
    working = WorkingCopy.objects.select_for_update().get(instruction=instruction)
    if not supplied:
        s.fail('PRECONDITION_REQUIRED', 'Для применения нужна текущая версия инструкции.', 428)
    newest = latest_transcript(ready_asset(instruction))
    if supplied != s.etag(working) or working.revision != proposal.base_working_revision or not newest or newest.id != proposal.transcript_id or proposal.applied_at:
        s.fail('STALE_PROPOSAL', 'Предложение устарело. Текущие шаги сохранены; создайте новое предложение.', 409, details={'actual_revision': working.revision})
    if not working.is_open or working.workflow != 'draft':
        s.fail('INVALID_STATE', 'Верните инструкцию в черновик перед применением ИИ.', 409)
    try:
        content = validate_proposal(proposal.content, transcript_data(proposal.transcript)['segments'], language=working.language, duration_ms=instruction.source_asset.duration_ms)
    except InvalidModelOutput:
        s.fail('MODEL_OUTPUT_INVALID', 'Предложение не прошло проверку. Текущие шаги сохранены.', 422)
    if not content['steps']:
        s.fail('NO_PROPOSED_STEPS', 'В предложении нет шагов для применения. Текущие шаги сохранены.', 422)
    warning_codes = sorted({warning['code'] for warning in content['warnings']})
    working.steps.all().delete()
    WorkingStep.objects.bulk_create([WorkingStep(organization=member.organization, working_copy=working, ordinal=index + 1, origin='ai', evidence_transcript_revision=proposal.transcript.revision, warning_codes=warning_codes, **step) for index, step in enumerate(content['steps'])])
    s.bump(working)
    proposal.applied_at, proposal.applied_by = timezone.now(), member.user
    proposal.save(update_fields=['applied_at', 'applied_by', 'updated_at'])
    s.audit(member, 'draft_proposal.applied', proposal)
    return s.working_data(working), 200
