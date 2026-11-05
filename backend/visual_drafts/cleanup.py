"""Bounded maintenance; applied uploaded evidence is retained with its instruction."""
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from core.models import Organization
from media_pipeline.models import ProcessingJob
from .models import VisualSession
from .services import safe_frame_path


def cleanup_visual_sessions(*, limit=50):
    now = timezone.now()
    expired_ids = list(VisualSession.objects.filter(state='active', expires_at__lte=now).values_list('id', flat=True)[:limit])
    expired, purged = 0, 0
    for session_id in expired_ids:
        with transaction.atomic():
            source = VisualSession.objects.get(pk=session_id)
            Organization.objects.select_for_update().get(pk=source.organization_id)
            session = VisualSession.objects.select_for_update().get(pk=session_id)
            if session.state == 'active' and session.expires_at <= now:
                session.state = 'expired'
                session.save(update_fields=['state', 'updated_at'])
                jobs = ProcessingJob.objects.filter(organization_id=session.organization_id, visual_session_id=session.id)
                jobs.filter(state='queued').update(state='cancelled', stage='cancelled', cancel_requested=True, retryable=False, finished_at=now)
                jobs.filter(state='running').update(cancel_requested=True)
                expired += 1
    candidates = VisualSession.objects.filter(frames_purged_at__isnull=True).filter(Q(mode='live', expires_at__lte=now - timedelta(hours=1)) | Q(mode='upload', created_at__lte=now - timedelta(days=7))).exclude(state='active')
    for session_id in list(candidates.values_list('id', flat=True)[:limit]):
        with transaction.atomic():
            source = VisualSession.objects.get(pk=session_id)
            Organization.objects.select_for_update().get(pk=source.organization_id)
            session = VisualSession.objects.select_for_update().get(pk=session_id)
            if session.frames_purged_at or session.proposals.filter(applied_at__isnull=False).exists() or ProcessingJob.objects.filter(visual_session_id=session.id, state__in=['queued', 'running']).exists():
                continue
            paths = [safe_frame_path(frame.storage_key) for frame in session.frames.all()]
            session.frames.all().delete()
            session.frames_purged_at = now
            session.save(update_fields=['frames_purged_at', 'updated_at'])
            transaction.on_commit(lambda paths=paths: [path.unlink(missing_ok=True) for path in paths])
            purged += 1
    return {'expired': expired, 'purged': purged}
