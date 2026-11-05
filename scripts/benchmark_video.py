"""Local-only extraction comparison in a disposable database and media directory.

No application .env is loaded and no model is invoked. Inputs must be prepared
playbacks, not quarantined user uploads. A sampling hit is NOT action recognition.
"""
import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = ('uniform_legacy', 'uniform', 'events')


def executable(name, explicit):
    candidates = [explicit, shutil.which(name), Path.home() / '.agents/skills/video-shotcraft/.runtime/bin' / name]
    for value in candidates:
        if value and Path(value).is_file() and os.access(value, os.X_OK):
            return str(value)
    raise SystemExit(f'Set --{name} to an installed executable.')


def command(argv, timeout=120):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout)


def synthetic_clips(root, ffmpeg):
    """Deterministic transport/sampling fixtures, not independent work processes."""
    specs = [
        ('short-local-change', "drawbox=x=210:y=120:w=100:h=90:color=white:t=fill:enable='between(t,1.1,1.7)'",
         [{'id': 'brief-visible-object', 'start_ms': 1100, 'end_ms': 1701}]),
        ('static-control', 'null', []),
        ('slow-change', "drawbox=x=210:y=120:w=100:h=90:color=white:t=fill,fade=t=in:st=0:d=25", []),
    ]
    entries = []
    for name, filters, events in specs:
        path = root / f'{name}.mp4'
        command([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=gray:s=640x360:r=24:d=30',
                 '-vf', filters, '-an', '-c:v', 'libx264', '-threads', '2', '-crf', '23', '-pix_fmt', 'yuv420p', str(path)])
        entries.append({'id': name, 'path': path, 'kind': 'synthetic_sampling_fixture', 'events': events})
    return entries


def percentile95(values):
    # Nearest rank is explicit; three repetitions do not establish a population p95.
    return sorted(values)[max(0, math.ceil(len(values) * .95) - 1)]


def event_coverage(events, times):
    return [{'id': str(event['id']), 'start_ms': event['start_ms'], 'end_ms': event['end_ms'],
             'sample_inside': any(event['start_ms'] <= t < event['end_ms'] for t in times),
             'sample_before': any(t < event['start_ms'] for t in times),
             'sample_after': any(t >= event['end_ms'] for t in times)} for event in events]


def run(args, root):
    ffmpeg, ffprobe = executable('ffmpeg', args.ffmpeg), executable('ffprobe', args.ffprobe)
    entries = []
    for index, video in enumerate(args.video):
        entries.append({'id': f'video-{index + 1}', 'path': Path(video).resolve(), 'kind': 'unlabelled_video', 'events': []})
    if args.manifest:
        manifest_path = Path(args.manifest).resolve()
        data = json.loads(manifest_path.read_text())
        for entry in data['clips']:
            entry = dict(entry)
            entry['path'] = (manifest_path.parent / entry['path']).resolve()
            entries.append(entry)
    if args.synthetic:
        entries.extend(synthetic_clips(root, ffmpeg))
    if not entries:
        raise SystemExit('Provide --video, --manifest or --synthetic.')
    if len(entries) > 40 or len({entry['id'] for entry in entries}) != len(entries):
        raise SystemExit('Use at most 40 clips with distinct IDs.')

    sys.path.insert(0, str(ROOT / 'backend'))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    from django.conf import settings
    # These overrides precede Django app setup. The user's database is never opened.
    settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
    settings.PRIVATE_MEDIA_ROOT = root / 'private-media'
    settings.PRIVATE_MEDIA_ROOT.mkdir(mode=0o700)
    settings.FFMPEG_PATH, settings.FFPROBE_PATH = ffmpeg, ffprobe
    settings.STEP_DRAFT_BACKEND, settings.EXTERNAL_AI_ENABLED, settings.CODEX_TEST_MODE = '', False, False
    import django
    django.setup()
    from django.core.management import call_command
    call_command('migrate', verbosity=0, interactive=False)
    from ai_budget.models import AIUsageReservation
    from core.models import Instruction, MediaAsset, Membership, Organization, User
    from media_pipeline.frames import extract_video_frames
    from media_pipeline.media import local_input
    from media_pipeline.models import ProcessingJob
    from media_pipeline.worker import JobContext, claim_job
    from visual_drafts.planning import intervals
    organization = Organization.objects.create(name='Isolated local benchmark')
    user = User.objects.create_user(email='benchmark@example.invalid')
    Membership.objects.create(organization=organization, user=user, role='author')
    report = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'local_frame_preparation_only', 'cloud_requests': 0,
              'cache_protocol': 'fresh JobContext for each strategy/repetition; reused between windows; OS cache not cleared',
              'machine': {'os': platform.system(), 'release': platform.release(), 'architecture': platform.machine(),
                          'logical_cpus': os.cpu_count(), 'python': platform.python_version(),
                          'ffmpeg': command([ffmpeg, '-version']).stdout.splitlines()[0]},
              'repetitions': args.repetitions, 'clips': []}
    for index, entry in enumerate(entries):
        path = Path(entry['path'])
        if not path.is_file() or not 0 < path.stat().st_size <= 750 * 1024 * 1024:
            raise SystemExit(f'Clip {entry["id"]} missing or exceeds playback size limit.')
        key = f'clip-{index}.mp4'
        playback = settings.PRIVATE_MEDIA_ROOT / key
        shutil.copyfile(path, playback)
        if not 0 < playback.stat().st_size <= 750 * 1024 * 1024:
            raise SystemExit('Copied playback exceeds the size limit.')
        data = json.loads(command([ffprobe, '-v', 'error', *local_input(playback), '-show_streams', '-show_format', '-of', 'json']).stdout)
        stream = next(stream for stream in data['streams'] if stream.get('codec_type') == 'video')
        duration = round(float(data['format']['duration']) * 1000)
        if not 1 <= duration <= 600000 or stream['width'] > 1280 or stream['height'] > 1280:
            raise SystemExit('Use a prepared playback <=1280px and <=10 minutes, matching application media preparation.')
        events = entry.get('events', [])
        if not all(type(e.get('start_ms')) is int and type(e.get('end_ms')) is int
                   and 0 <= e['start_ms'] < e['end_ms'] <= duration and 'id' in e for e in events):
            raise SystemExit('Event intervals must lie within the clip.')
        with playback.open('rb') as file:
            digest = hashlib.file_digest(file, 'sha256').hexdigest()
        asset = MediaAsset.objects.create(organization=organization, storage_key=key, sha256=digest,
                                         status='ready', duration_ms=duration, width=stream['width'], height=stream['height'], is_demo=False)
        instruction = Instruction.objects.create(organization=organization, owner=user, source_asset=asset)
        clip = {'id': entry['id'], 'kind': entry.get('kind', 'unlabelled_video'), 'sha256': digest,
                'duration_ms': duration, 'width': asset.width, 'height': asset.height, 'runs': []}
        if entry.get('source_url'):
            clip['source_url'] = entry['source_url']
        for repetition in range(args.repetitions):
            # Counterbalance order to reduce systematic warm OS-cache bias.
            order = STRATEGIES[repetition % 3:] + STRATEGIES[:repetition % 3]
            for strategy in order:
                ProcessingJob.objects.create(organization=organization, instruction=instruction, asset=asset,
                                             requested_by=user, kind='analyze_video')
                job = claim_job()
                context = JobContext(job)
                calls = {'ffmpeg': 0, 'ffprobe': 0}
                original_run = context.run
                def counted_run(argv, timeout, **kwargs):
                    if argv[0] == ffmpeg:
                        calls['ffmpeg'] += 1
                    if argv[0] == ffprobe:
                        calls['ffprobe'] += 1
                    return original_run(argv, timeout, **kwargs)
                context.run = counted_run
                windows, times = [], []
                started = time.perf_counter()
                for start_ms, end_ms in intervals(0, duration):
                    with extract_video_frames(asset, context, start_ms=start_ms, end_ms=end_ms, count=12, strategy=strategy) as batch:
                        manifest = batch.manifest()
                        assert 1 <= len(batch.frames) <= 12
                        windows.append({k: manifest[k] for k in ('start_ms', 'end_ms', 'sampling_version', 'frame_count',
                                       'largest_unsampled_interval_us', 'processing', 'selection') if k in manifest})
                        windows[-1]['timestamps_ms'] = [frame.timestamp_ms for frame in batch.frames]
                        times.extend(windows[-1]['timestamps_ms'])
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                ProcessingJob.objects.filter(pk=job.pk).update(state='done')
                result = {'strategy': strategy, 'repetition': repetition + 1, 'wall_ms': elapsed_ms,
                          'processes': calls, 'windows': windows, 'event_sampling': event_coverage(events, times)}
                clip['runs'].append(result)
                print(f'{entry["id"]} {strategy} #{repetition + 1}: {elapsed_ms:.0f} ms, {calls}', flush=True)
        clip['summary'] = {}
        for strategy in STRATEGIES:
            values = [item['wall_ms'] for item in clip['runs'] if item['strategy'] == strategy]
            clip['summary'][strategy] = {'median_ms': round(statistics.median(values), 3),
                                        'p95_nearest_rank_ms': percentile95(values), 'n': len(values)}
        baseline = clip['summary']['uniform_legacy']['median_ms']
        clip['summary']['events_vs_legacy_reduction_pct'] = round(100 * (1 - clip['summary']['events']['median_ms'] / baseline), 2)
        report['clips'].append(clip)
    assert AIUsageReservation.objects.count() == 0
    report['limitations'] = ['No cloud model or human correction measured.', 'Sampling an event is not recognizing it.',
                             'Synthetic fixtures are not a diverse real-world accuracy dataset.',
                             'Small repeat counts describe this machine/run, not production p95.']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', action='append', default=[], help='Prepared playback to compare (may repeat).')
    parser.add_argument('--manifest', help='JSON clips with id, relative path, kind and optional labelled events.')
    parser.add_argument('--synthetic', action='store_true', help='Include deterministic short change/static/slow controls.')
    parser.add_argument('--repetitions', type=int, choices=range(1, 11), default=3)
    parser.add_argument('--ffmpeg')
    parser.add_argument('--ffprobe')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        raise SystemExit('Output already exists; choose a new file to preserve previous evidence.')
    with tempfile.TemporaryDirectory(prefix='video-benchmark-') as directory:
        report = run(args, Path(directory))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
        file.write('\n')
    print(f'Report: {output}')


if __name__ == '__main__':
    main()
