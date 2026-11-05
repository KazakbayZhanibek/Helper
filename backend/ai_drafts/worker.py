"""Durable jobs produce independent artifacts; only apply_proposal edits steps."""
import json
import tempfile
import sys
from pathlib import Path

from django.conf import settings
from django.db import transaction

from core.models import Instruction, Membership, WorkingCopy
from media_pipeline.exceptions import ProcessingFailure
from .models import DraftProposal, Transcript, TranscriptSegment
from .providers import ProviderFailure, StepDraftProvider, asr_configuration, setting
from .services import latest_transcript, transcript_data
from .validation import InvalidModelOutput, parse_json, validate_proposal


def active_source(job, *, lock=False):
    query = Instruction.objects.filter(id=job.instruction_id, organization_id=job.organization_id, organization__status='active', lifecycle='active', source_asset_id=job.asset_id, source_asset__status='ready').select_related('source_asset', 'organization')
    if lock:
        query = query.select_for_update(of=('self',))
    instruction = query.first()
    if not instruction or not Membership.objects.filter(organization_id=job.organization_id, user_id=job.requested_by_id, user__is_active=True, is_active=True, role__in=['admin', 'author', 'reviewer']).exists():
        raise ProcessingFailure('SOURCE_UNAVAILABLE', 'Источник или доступ к нему больше недоступен.')
    return instruction


def validate_transcript_segments(value, duration_ms):
    if not isinstance(value, dict) or not isinstance(value.get('segments'), list):
        raise ProcessingFailure('TRANSCRIPT_INVALID', 'Распознавание вернуло некорректную расшифровку.')
    segments = value['segments']
    if not segments:
        raise ProcessingFailure('NO_SPEECH', 'Разборчивая речь не обнаружена. Видео доступно для ручной разметки.')
    if len(segments) > 300:
        raise ProcessingFailure('TRANSCRIPT_SIZE_LIMIT', 'В расшифровке больше 300 фрагментов. Доступна ручная работа с видео.')
    result, previous_start, total = [], -1, 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise ProcessingFailure('TRANSCRIPT_INVALID', 'Не удалось проверить фрагменты речи.')
        start, end, text = segment.get('start_ms'), segment.get('end_ms'), segment.get('text')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= duration_ms or start < previous_start or not isinstance(text, str) or not text.strip():
            raise ProcessingFailure('TRANSCRIPT_INVALID', 'Не удалось проверить время или текст распознанных фрагментов.')
        text = text.strip()
        total += len(text)
        if total > 18000:
            raise ProcessingFailure('TRANSCRIPT_SIZE_LIMIT', 'Расшифровка длиннее 18 000 символов. Доступна ручная работа с видео.')
        result.append({'ordinal': len(result) + 1, 'start_ms': start, 'end_ms': end, 'raw_text': text, 'text': text})
        previous_start = start
    return result


def transcribe_local(asset, language, context):
    config = asr_configuration()
    if not config['available']:
        raise ProcessingFailure('ASR_UNAVAILABLE', config['reason'])
    root = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
    audio = (root / asset.audio_key).resolve()
    if not asset.has_audio or not asset.audio_key or not audio.is_relative_to(root) or not audio.is_file():
        raise ProcessingFailure('NO_AUDIO', 'Звуковая дорожка недоступна. Добавьте шаги вручную.')
    if audio.stat().st_size > 30 * 1024 * 1024:
        raise ProcessingFailure('ASR_AUDIO_INVALID', 'Подготовленная звуковая дорожка превышает лимит.')
    context.stage('transcribing')
    if config['provider'] == 'whisper_cpp':
        # Attempt-local output, no user filenames and no command shell.
        with tempfile.TemporaryDirectory(prefix='master-asr-') as directory:
            output_prefix = Path(directory) / 'transcript'
            command = [setting('WHISPER_CPP_BINARY'), '-m', setting('ASR_MODEL_PATH'), '-f', str(audio), '-l', 'auto' if language == 'mixed' else language, '-oj', '-of', str(output_prefix), '-t', '2']
            if str(setting('ASR_NO_GPU', '0')).lower() in {'1', 'true'}:
                command.append('-ng')
            result = context.run(command, timeout=1800)
            target = output_prefix.with_suffix('.json')
            if result.returncode or not target.is_file() or target.stat().st_size > 262144:
                raise ProcessingFailure('ASR_PROCESS_FAILED', 'Не удалось распознать речь. Проверьте локальную модель и повторите.')
            try:
                raw = parse_json(target.read_text(encoding='utf-8'))
                value = {'segments': [{'start_ms': item['offsets']['from'], 'end_ms': item['offsets']['to'], 'text': item['text']} for item in raw['transcription']]}
            except (KeyError, TypeError, UnicodeError, InvalidModelOutput):
                raise ProcessingFailure('TRANSCRIPT_INVALID', 'Распознавание вернуло некорректную расшифровку.') from None
    else:
        command = [setting('ASR_PYTHON', sys.executable), str(Path(__file__).with_name('asr_runner.py')), '--backend', config['provider'], '--model', setting('ASR_MODEL_PATH'), '--audio', str(audio), '--language', language]
        result = context.run(command, timeout=1800)
        if result.returncode:
            raise ProcessingFailure('ASR_PROCESS_FAILED', 'Не удалось распознать речь. Проверьте локальную модель и повторите.')
        try:
            value = parse_json(result.stdout)
        except InvalidModelOutput:
            raise ProcessingFailure('TRANSCRIPT_INVALID', 'Распознавание вернуло некорректную расшифровку.') from None
    context.check()
    return validate_transcript_segments(value, asset.duration_ms), config


def transcribe_job(job, context):
    existing = Transcript.objects.filter(source_job_id=job.id, organization_id=job.organization_id, asset_id=job.asset_id).first()
    if existing:
        context.check()
        return {'transcript_id': str(existing.id), 'transcript_revision': existing.revision}
    instruction = active_source(job)
    language = job.payload.get('language')
    if language not in {'ru', 'kk', 'mixed'}:
        raise ProcessingFailure('JOB_INPUT_INVALID', 'Язык задания недействителен.')
    segments, config = transcribe_local(instruction.source_asset, language, context)
    context.stage('saving_transcript')
    with transaction.atomic():
        context.commit_guard()
        instruction = active_source(job, lock=True)
        working = WorkingCopy.objects.select_for_update().get(instruction=instruction)
        if not working.is_open or working.workflow != 'draft' or working.language != language:
            raise ProcessingFailure('SOURCE_CHANGED', 'Рабочая версия изменилась. Верните её в черновик и повторите распознавание.')
        existing = Transcript.objects.filter(source_job_id=job.id).first()
        if existing:
            return {'transcript_id': str(existing.id), 'transcript_revision': existing.revision}
        latest = latest_transcript(instruction.source_asset)
        warnings = [{'code': 'experimental_language', 'message': 'Качество распознавания казахской и смешанной речи требует проверки человеком.'}] if language in {'kk', 'mixed'} else []
        transcript = Transcript.objects.create(organization_id=job.organization_id, asset=instruction.source_asset, source_job_id=job.id, revision=(latest.revision + 1) if latest else 1, language=language, provider=config['provider'], model=config['model'], duration_ms=instruction.source_asset.duration_ms, warnings=warnings, input_sha256=job.input_hash)
        TranscriptSegment.objects.bulk_create([TranscriptSegment(organization_id=job.organization_id, transcript=transcript, **segment) for segment in segments])
        changed = False
        for step in working.steps.all():
            if step.evidence_segment_ids:
                step.checked_at = step.checked_by = None
                step.warning_codes = sorted(set(step.warning_codes or []) | {'source_changed'})
                step.save(update_fields=['checked_at', 'checked_by', 'warning_codes', 'updated_at'])
                changed = True
        if changed:
            from core.services import bump
            bump(working)
        return {'transcript_id': str(transcript.id), 'transcript_revision': transcript.revision, 'working_revision': working.revision}


def draft_job(job, context):
    existing = DraftProposal.objects.filter(source_job_id=job.id, organization_id=job.organization_id, instruction_id=job.instruction_id).first()
    if existing:
        context.check()
        return {'proposal_id': str(existing.id)}
    instruction = active_source(job)
    transcript = Transcript.objects.filter(id=job.payload.get('transcript_id'), organization_id=job.organization_id, asset=instruction.source_asset, revision=job.payload.get('transcript_revision')).first()
    working = instruction.working_copy
    if not transcript or not working.is_open or working.workflow != 'draft' or working.revision != job.payload.get('base_working_revision') or latest_transcript(instruction.source_asset).id != transcript.id:
        raise ProcessingFailure('STALE_PROPOSAL', 'Исходная версия изменилась. Создайте новое предложение.')
    segments = transcript_data(transcript)['segments']
    if not segments or len(segments) > 300 or sum(len(s['text']) for s in segments) > 18000:
        raise ProcessingFailure('TRANSCRIPT_SIZE_LIMIT', 'Размер расшифровки не подходит для автоматического создания шагов.')
    request = {'schema_version': '1', 'language': working.language, 'duration_ms': transcript.duration_ms, 'transcript_id': str(transcript.id), 'transcript_revision': transcript.revision, 'segments': [{key: segment[key] for key in ('id', 'ordinal', 'start_ms', 'end_ms', 'text')} for segment in segments]}
    context.stage('generating_steps')
    try:
        from ai_budget.services import execute_budgeted_call
        provider = StepDraftProvider(instruction.organization)
        result = execute_budgeted_call(
            provider, job=job, context=context, call_key='transcript-draft',
            invoke=lambda: provider.generate(request, context=context) if provider.provider == 'codex' else provider.generate(request),
        )
    except ProviderFailure as error:
        raise ProcessingFailure(error.code, error.message, retryable=error.retryable) from None
    context.check()
    context.stage('validating_steps')
    try:
        content = validate_proposal(result, segments, language=working.language, duration_ms=transcript.duration_ms)
    except InvalidModelOutput as error:
        raise ProcessingFailure(str(error), 'Предложение ИИ не прошло проверку источников, времени или параметров. Рабочие шаги сохранены.') from None
    with transaction.atomic():
        context.commit_guard()
        instruction = active_source(job, lock=True)
        current = WorkingCopy.objects.select_for_update().get(instruction=instruction)
        newest = latest_transcript(instruction.source_asset)
        # Save a validated proposal even if editing made it stale while the
        # provider ran. Preview marks it stale; applying it is prohibited.
        if not newest or newest.id != transcript.id or not current.is_open:
            raise ProcessingFailure('STALE_PROPOSAL', 'Исходная расшифровка изменилась. Создайте новое предложение.')
        existing = DraftProposal.objects.filter(source_job_id=job.id).first()
        proposal = existing or DraftProposal.objects.create(organization_id=job.organization_id, instruction=instruction, transcript=transcript, source_job_id=job.id, base_working_revision=job.payload['base_working_revision'], provider=provider.provider, model=provider.model, content=content, input_sha256=job.input_hash)
        return {'proposal_id': str(proposal.id)}


def process_job(job, context):
    context.check()
    if job.kind == 'transcribe':
        return transcribe_job(job, context)
    if job.kind == 'draft_steps':
        return draft_job(job, context)
    raise ProcessingFailure('JOB_KIND_INVALID', 'Неизвестный тип задания ИИ.')
