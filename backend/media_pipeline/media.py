import json
import hashlib
import math
import shutil
from pathlib import Path

from django.conf import settings
from django.db import transaction

from core.models import MediaAsset
from .exceptions import ProcessingFailure
from .storage import private_path, make_private_parent


def binary(name):
    configured = getattr(settings, name.upper() + '_PATH', '')
    executable = (shutil.which(str(configured)) or str(configured)) if configured else shutil.which(name)
    if not executable or not Path(executable).is_file():
        raise ProcessingFailure('MEDIA_RUNTIME_UNAVAILABLE', 'FFmpeg/FFprobe не настроен на сервере.', retryable=True)
    return executable


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def validate_probe(data):
    if not isinstance(data, dict) or not isinstance(data.get('streams'), list):
        raise ProcessingFailure('INVALID_MEDIA', 'Не удалось прочитать структуру видео.')
    streams = data['streams']
    video = [stream for stream in streams if stream.get('codec_type') == 'video']
    audio = [stream for stream in streams if stream.get('codec_type') == 'audio']
    if len(video) != 1 or len(audio) > 1 or any(stream.get('codec_type') not in ('video', 'audio', 'data') for stream in streams):
        raise ProcessingFailure('UNSUPPORTED_STREAMS', 'Нужен один видеопоток и не больше одной звуковой дорожки.')
    video = video[0]
    format_names = set(data.get('format', {}).get('format_name', 'mov,mp4').split(','))
    webm = bool(format_names & {'webm', 'matroska'})
    if not webm and not format_names & {'mov', 'mp4'}:
        raise ProcessingFailure('UNSUPPORTED_CONTAINER', 'Поддерживаются контейнеры MP4, MOV и WebM.')
    if video.get('codec_name') not in (('vp8', 'vp9') if webm else ('h264', 'hevc')):
        raise ProcessingFailure('UNSUPPORTED_VIDEO_CODEC', 'Поддерживаются H.264/HEVC в MP4/MOV и VP8/VP9 в WebM.')
    audio_codecs = ('opus',) if webm else ('aac', 'pcm_s16le', 'pcm_s24le', 'pcm_s32le', 'pcm_s16be', 'pcm_s24be', 'pcm_s32be', 'pcm_f32le', 'pcm_f64le')
    if audio and audio[0].get('codec_name') not in audio_codecs:
        raise ProcessingFailure('UNSUPPORTED_AUDIO_CODEC', 'Поддерживается звук AAC/PCM в MP4/MOV и Opus в WebM.')
    try:
        width, height = int(video['width']), int(video['height'])
    except (KeyError, ValueError, TypeError):
        raise ProcessingFailure('INVALID_RESOLUTION', 'Не удалось определить размер кадра.')
    if min(width, height) < 2 or max(width, height) > 3840 or min(width, height) > 2160:
        raise ProcessingFailure('INVALID_RESOLUTION', 'Максимальное разрешение видео — 3840×2160.')
    duration = _finite_number(data.get('format', {}).get('duration'))
    video_duration = _finite_number(video.get('duration'))
    if webm and video_duration is None:
        # WebM stores duration at the container level instead of per stream.
        video_duration = duration
    if duration is None or video_duration is None:
        raise ProcessingFailure('INVALID_DURATION', 'Не удалось определить длительность видео.')
    minimum = getattr(settings, 'MEDIA_MIN_DURATION_MS', 10000)
    if duration * 1000 < minimum or duration > 600 or video_duration > 600:
        raise ProcessingFailure('INVALID_DURATION', 'Длительность видео должна быть от 10 секунд до 10 минут.')
    # Implausible timing disagreements can otherwise turn a small container into an unbounded decode.
    if video_duration <= 0 or abs(video_duration - duration) > 5:
        raise ProcessingFailure('INVALID_DURATION', 'Повреждены временные метки видео.')
    rotation = video.get('tags', {}).get('rotate', 0)
    for side in video.get('side_data_list', []):
        if 'rotation' in side:
            rotation = side['rotation']
    rotation = _finite_number(rotation) or 0
    if abs(round(rotation)) % 180 == 90:
        width, height = height, width
    return {'duration_ms': round(duration * 1000), 'width': width, 'height': height, 'has_audio': bool(audio)}


def local_input(path):
    # Restricted local containers only; playlists and network protocols are rejected.
    with Path(path).open('rb') as source:
        webm = source.read(4) == b'\x1aE\xdf\xa3'
    options = ['-protocol_whitelist', 'file,pipe', '-format_whitelist', 'matroska,webm' if webm else 'mov']
    if not webm:
        options += ['-enable_drefs', '0', '-use_absolute_path', '0']
    return [*options, '-i', str(path)]


def probe(path, context):
    result = context.run([binary('ffprobe'), '-v', 'error', *local_input(path), '-show_streams', '-show_format', '-of', 'json'], timeout=30)
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise ProcessingFailure('INVALID_MEDIA', 'Файл не является поддерживаемым видео.')
    # MediaRecorder produces non-seekable WebM without a container Duration.
    # Demux a bounded timestamp index instead of trusting a client duration hint.
    format_data = data.get('format', {})
    if set(format_data.get('format_name', '').split(',')) & {'webm', 'matroska'} and _finite_number(format_data.get('duration')) is None:
        result = context.run([binary('ffprobe'), '-v', 'error', *local_input(path), '-select_streams', 'v:0',
            '-show_entries', 'packet=pts_time,duration_time', '-of', 'csv=p=0'], timeout=30)
        packet_times = []
        for row in result.stdout.splitlines():
            fields = row.split(',')
            if len(fields) != 2:
                raise ProcessingFailure('INVALID_DURATION', 'Не удалось проверить длительность записи камеры.')
            pts, duration = (_finite_number(value) for value in fields)
            if pts is None or (duration is not None and duration <= 0) or len(packet_times) >= 200000 or pts < -1 or pts + (duration or 0) > 601:
                raise ProcessingFailure('INVALID_DURATION', 'Временные метки записи камеры выходят за допустимые пределы.')
            packet_times.append((pts, duration))
        if not packet_times:
            raise ProcessingFailure('INVALID_DURATION', 'В записи камеры нет видеокадров.')
        packet_times.sort()
        first = packet_times[0][0]
        last_pts, last_duration = packet_times[-1]
        if last_duration is None:
            earlier = [pts for pts, _ in packet_times if pts < last_pts]
            if not earlier:
                raise ProcessingFailure('INVALID_DURATION', 'Недостаточно временных меток для проверки записи камеры.')
            # MediaRecorder may omit DefaultDuration as well as Duration. The
            # final observed presentation interval is the conservative end estimate.
            last_duration = last_pts - earlier[-1]
        last = max([pts + (duration or 0) for pts, duration in packet_times] + [last_pts + last_duration])
        format_data['duration'] = last - first
    return validate_probe(data)


def prepare_media(job, context):
    asset = job.asset
    source = private_path(asset.original_key)
    if not source.is_file() or source.stat().st_size != asset.original_size_bytes:
        raise ProcessingFailure('SOURCE_MISSING', 'Исходный файл отсутствует или повреждён.')
    context.stage('validating')
    digest = hashlib.sha256()
    with source.open('rb') as file:
        while chunk := file.read(1024 * 1024):
            context.check()
            digest.update(chunk)
    if digest.hexdigest() != asset.sha256:
        raise ProcessingFailure('SOURCE_CHANGED', 'Исходный файл повреждён после загрузки.')
    with transaction.atomic():
        context.commit_guard()
        MediaAsset.objects.filter(pk=asset.id).update(status='validating')
    info = probe(source, context)
    prefix = f'derived/{job.organization_id}/{asset.id}/{context.lease_token}'
    video_key, thumbnail_key = prefix + '/playback.mp4', prefix + '/thumbnail.jpg'
    audio_key = prefix + '/audio.wav' if info['has_audio'] else ''
    playback, thumbnail = private_path(video_key), private_path(thumbnail_key)
    make_private_parent(playback)
    ffmpeg = binary('ffmpeg')
    common = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-threads', '2']
    scale = "scale=w='if(gte(iw,ih),min(1280,iw),min(720,iw))':h='if(gte(iw,ih),min(720,ih),min(1280,ih))':force_original_aspect_ratio=decrease:force_divisible_by=2"
    try:
        context.stage('transcoding')
        context.run([*common, *local_input(source), '-t', '600', '-map', '0:v:0', '-map', '0:a:0?',
            '-vf', scale, '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k', '-map_metadata', '-1', '-movflags', '+faststart', '-fs', str(750 * 1024 ** 2), str(playback)], timeout=1200)
        context.stage('thumbnail')
        context.run([*common, *local_input(playback), '-ss', '1', '-map', '0:v:0', '-frames:v', '1', '-vf', 'scale=640:-2', '-q:v', '3', str(thumbnail)], timeout=60)
        if audio_key:
            context.stage('extracting_audio')
            context.run([*common, *local_input(source), '-t', '600', '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
                '-c:a', 'pcm_s16le', '-map_metadata', '-1', str(private_path(audio_key))], timeout=180)
        output_info = probe(playback, context)
        if abs(output_info['duration_ms'] - info['duration_ms']) > 1000:
            raise ProcessingFailure('INCOMPLETE_DERIVATIVE', 'Подготовленная копия видео неполная.')
        if not thumbnail.is_file() or thumbnail.stat().st_size == 0:
            raise ProcessingFailure('MEDIA_PROCESSING_FAILED', 'Не удалось создать обложку.', retryable=True)
        for path in playback.parent.iterdir():
            path.chmod(0o600)
        with transaction.atomic():
            context.commit_guard()
            locked = MediaAsset.objects.select_for_update().get(pk=asset.id)
            locked.storage_key, locked.thumbnail_key, locked.audio_key = video_key, thumbnail_key, audio_key
            locked.status, locked.duration_ms = 'ready', info['duration_ms']
            locked.width, locked.height, locked.has_audio = output_info['width'], output_info['height'], info['has_audio']
            locked.save()
        return {'asset_id': str(asset.id), 'duration_ms': info['duration_ms'], 'has_audio': info['has_audio'], 'manual_allowed': True}
    except BaseException:
        # A signal can arrive immediately after commit. Never remove a derivative
        # that is already referenced by the durable asset record.
        try:
            referenced = MediaAsset.objects.filter(pk=asset.id, storage_key=video_key).exists()
        except Exception:
            referenced = True  # Cleanup can retry later when the database is available.
        if not referenced:
            shutil.rmtree(playback.parent, ignore_errors=True)
        raise
