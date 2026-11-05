from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from core.models import TenantEntity


class Transcript(TenantEntity):
    source_job_id = models.UUIDField(null=True, unique=True)
    asset = models.ForeignKey('core.MediaAsset', on_delete=models.CASCADE, related_name='transcripts')
    revision = models.PositiveIntegerField()
    language = models.CharField(max_length=5)
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=200)
    duration_ms = models.PositiveIntegerField()
    warnings = models.JSONField(default=list)
    input_sha256 = models.CharField(max_length=64)
    config_version = models.CharField(max_length=32, default='asr-v1')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['asset', 'revision'], name='transcript_asset_revision'), models.CheckConstraint(condition=Q(revision__gte=1), name='transcript_positive_revision')]
        ordering = ['-revision']

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Transcript revisions are immutable.')
        if self.organization_id != self.asset.organization_id:
            raise ValidationError('Transcript and asset organizations must match.')
        return super().save(*args, **kwargs)


class TranscriptSegment(TenantEntity):
    transcript = models.ForeignKey(Transcript, on_delete=models.CASCADE, related_name='segments')
    ordinal = models.PositiveIntegerField()
    start_ms = models.PositiveIntegerField()
    end_ms = models.PositiveIntegerField()
    raw_text = models.TextField()
    text = models.TextField()

    class Meta:
        ordering = ['ordinal']
        constraints = [models.UniqueConstraint(fields=['transcript', 'ordinal'], name='transcript_segment_order'), models.CheckConstraint(condition=Q(end_ms__gt=models.F('start_ms')), name='transcript_segment_interval')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Transcript segments are immutable.')
        if self.organization_id != self.transcript.organization_id:
            raise ValidationError('Segment and transcript organizations must match.')
        return super().save(*args, **kwargs)


class DraftProposal(TenantEntity):
    source_job_id = models.UUIDField(null=True, unique=True)
    instruction = models.ForeignKey('core.Instruction', on_delete=models.CASCADE, related_name='draft_proposals')
    transcript = models.ForeignKey(Transcript, on_delete=models.CASCADE, related_name='proposals')
    base_working_revision = models.PositiveIntegerField()
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=200)
    content = models.JSONField()
    input_sha256 = models.CharField(max_length=64)
    config_version = models.CharField(max_length=32, default='steps-v1')
    applied_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.ForeignKey('core.User', null=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        ordering = ['-created_at']
        constraints = [models.CheckConstraint(condition=Q(base_working_revision__gte=1), name='proposal_positive_revision')]

    def save(self, *args, **kwargs):
        if self.organization_id != self.instruction.organization_id or self.organization_id != self.transcript.organization_id or self.instruction.source_asset_id != self.transcript.asset_id:
            raise ValidationError('Proposal sources must belong to its instruction and organization.')
        return super().save(*args, **kwargs)
