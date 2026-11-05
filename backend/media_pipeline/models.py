import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantEntity


class MediaQuota(models.Model):
    organization = models.OneToOneField('core.Organization', primary_key=True, on_delete=models.CASCADE)
    limit_bytes = models.PositiveBigIntegerField(default=20 * 1024 ** 3)
    reserved_bytes = models.PositiveBigIntegerField(default=0)
    used_bytes = models.PositiveBigIntegerField(default=0)


class UploadSession(TenantEntity):
    instruction = models.ForeignKey('core.Instruction', on_delete=models.CASCADE, related_name='uploads')
    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    filename = models.CharField(max_length=255)
    language = models.CharField(max_length=5, default='ru')
    expected_size_bytes = models.PositiveBigIntegerField()
    received_bytes = models.PositiveBigIntegerField(default=0)
    reserved_bytes = models.PositiveBigIntegerField(default=0)
    state = models.CharField(max_length=16, default='pending')
    expires_at = models.DateTimeField()
    asset = models.OneToOneField('core.MediaAsset', null=True, on_delete=models.PROTECT, related_name='upload_session')
    error_code = models.CharField(max_length=64, default='', blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(state__in=['pending', 'uploading', 'completed', 'failed', 'cancelled', 'expired']), name='upload_valid_state')]
        indexes = [models.Index(fields=['state', 'expires_at'])]


class ProcessingJob(TenantEntity):
    instruction = models.ForeignKey('core.Instruction', on_delete=models.CASCADE, related_name='processing_jobs')
    asset = models.ForeignKey('core.MediaAsset', null=True, blank=True, on_delete=models.PROTECT, related_name='processing_jobs')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=20)
    state = models.CharField(max_length=12, default='queued')
    stage = models.CharField(max_length=64, default='queued')
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    payload = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    input_hash = models.CharField(max_length=64, default='', blank=True)
    config_version = models.CharField(max_length=64, default='v1')
    error_code = models.CharField(max_length=64, default='', blank=True)
    error_summary = models.CharField(max_length=500, default='', blank=True)
    retryable = models.BooleanField(default=False)
    cancel_requested = models.BooleanField(default=False)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=['prepare_media', 'transcribe', 'draft_steps', 'analyze_video', 'analyze_live']), name='job_valid_kind'),
            models.CheckConstraint(condition=Q(kind='analyze_live', asset__isnull=True) | (~Q(kind='analyze_live') & Q(asset__isnull=False)), name='job_asset_kind'),
            models.CheckConstraint(condition=Q(state__in=['queued', 'running', 'done', 'failed', 'cancelled']), name='job_valid_state'),
            models.UniqueConstraint(fields=['instruction', 'kind'], condition=Q(state__in=['queued', 'running']), name='unique_active_instruction_job'),
        ]
        indexes = [models.Index(fields=['state', 'created_at'])]
