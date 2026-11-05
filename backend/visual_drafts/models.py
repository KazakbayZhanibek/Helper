from django.db import models
from django.db.models import Q
from core.models import TenantEntity


class VisualSession(TenantEntity):
    instruction = models.ForeignKey('core.Instruction', on_delete=models.CASCADE, related_name='visual_sessions')
    asset = models.ForeignKey('core.MediaAsset', null=True, on_delete=models.CASCADE, related_name='visual_sessions')
    created_by = models.ForeignKey('core.User', on_delete=models.PROTECT)
    mode = models.CharField(max_length=8)
    state = models.CharField(max_length=12, default='active')
    consent_images = models.BooleanField(default=False)
    base_working_revision = models.PositiveIntegerField()
    language = models.CharField(max_length=5)
    expires_at = models.DateTimeField()
    last_analysis_at = models.DateTimeField(null=True)
    last_call_at = models.DateTimeField(null=True)
    analysis_calls = models.PositiveSmallIntegerField(default=0)
    window_start_ms = models.PositiveIntegerField(default=0)
    window_end_ms = models.PositiveIntegerField(default=600000)
    frames_purged_at = models.DateTimeField(null=True)
    source_manifest = models.JSONField(default=dict)
    analysis_scope = models.CharField(max_length=8, default='window')
    analysis_budget = models.JSONField(default=dict)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(mode__in=['live', 'upload']), name='visual_session_mode'), models.CheckConstraint(condition=Q(state__in=['active', 'completed', 'cancelled', 'expired']), name='visual_session_state'), models.UniqueConstraint(fields=['instruction'], condition=Q(mode='live', state='active'), name='one_active_live_session')]


class FrameEvidence(TenantEntity):
    session = models.ForeignKey(VisualSession, on_delete=models.CASCADE, related_name='frames')
    sequence = models.PositiveIntegerField()
    timestamp_ms = models.PositiveIntegerField()
    coverage_start_ms = models.PositiveIntegerField()
    coverage_end_ms = models.PositiveIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    request_sha256 = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=128)
    storage_key = models.CharField(max_length=240)

    class Meta:
        ordering = ['sequence']
        constraints = [models.UniqueConstraint(fields=['session', 'sequence'], name='visual_frame_sequence'), models.UniqueConstraint(fields=['session', 'idempotency_key'], name='visual_frame_idempotency'), models.CheckConstraint(condition=Q(coverage_end_ms__gt=models.F('coverage_start_ms')), name='visual_frame_interval')]


class VisualProposal(TenantEntity):
    session = models.ForeignKey(VisualSession, on_delete=models.CASCADE, related_name='proposals')
    source_job_id = models.UUIDField(unique=True)
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=200)
    content = models.JSONField()
    applied_at = models.DateTimeField(null=True)
    applied_by = models.ForeignKey('core.User', null=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        ordering = ['-created_at']


class VisualAnalysisWindow(TenantEntity):
    """A paid-call checkpoint, independent of the mutable instruction draft."""
    session = models.ForeignKey(VisualSession, on_delete=models.CASCADE, related_name='analysis_windows')
    parent = models.ForeignKey('self', null=True, on_delete=models.CASCADE, related_name='refinements')
    ordinal = models.PositiveSmallIntegerField()
    start_ms = models.PositiveIntegerField()
    end_ms = models.PositiveIntegerField()
    state = models.CharField(max_length=12, default='pending')
    frame_ids = models.JSONField(default=list)
    source_manifest = models.JSONField(default=dict)
    content = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, default='', blank=True)

    class Meta:
        ordering = ['ordinal']
        constraints = [
            models.UniqueConstraint(fields=['session', 'ordinal'], name='visual_window_ordinal'),
            models.CheckConstraint(condition=Q(end_ms__gt=models.F('start_ms')), name='visual_window_interval'),
            models.CheckConstraint(condition=Q(state__in=['pending', 'running', 'completed', 'failed']), name='visual_window_state'),
        ]


class VisualAnalysisReview(TenantEntity):
    """A separate immutable result; the original window is never rewritten."""
    window = models.OneToOneField(VisualAnalysisWindow, on_delete=models.CASCADE, related_name='semantic_review')
    version = models.CharField(max_length=8, default='v1')
    state = models.CharField(max_length=12, default='running')
    content = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, default='', blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(state__in=['running', 'completed', 'failed', 'skipped']), name='visual_review_state')]
