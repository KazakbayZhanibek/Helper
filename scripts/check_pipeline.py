"""Read local pipeline readiness without revealing credentials or calling cloud AI."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from dev import load_environment

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--organization', type=uuid.UUID, default='7ea2d071-f15f-4fe9-b440-f1622b3e17cf')
    args = parser.parse_args()
    os.environ.update(load_environment())
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    sys.path.insert(0, str(ROOT / 'backend'))
    import django
    django.setup()
    from ai_drafts.providers import capabilities
    from visual_drafts.provider import capabilities as visual_capabilities
    from core.models import Organization
    from media_pipeline.exceptions import ProcessingFailure
    from media_pipeline.media import binary
    from media_pipeline.models import ProcessingJob
    organization = Organization.objects.filter(id=args.organization, status='active').first()
    if not organization:
        raise SystemExit('Active organization not found. Prepare the local database first.')
    result = capabilities(organization, probe=False)
    result['visual_analysis'] = visual_capabilities(organization)
    result['media'] = {}
    for name in ('ffmpeg', 'ffprobe'):
        try:
            binary(name)
            result['media'][name] = 'configured'
        except ProcessingFailure:
            result['media'][name] = 'unavailable'
    result['jobs'] = {state: ProcessingJob.objects.filter(organization=organization, state=state).count() for state in ('queued', 'running', 'failed')}
    result['note'] = 'Configuration only: no cloud request made and API-key validity not verified.'
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
