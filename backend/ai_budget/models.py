"""Private accounting records; amounts are integer USD microdollars or calls."""
import uuid
from django.db import models
from django.db.models import Q


class BudgetAccount(models.Model):
    key = models.CharField(max_length=180, primary_key=True)
    unit = models.CharField(max_length=16)
    reserved = models.PositiveBigIntegerField(default=0)
    spent = models.PositiveBigIntegerField(default=0)
    # Sticky circuit breaker if provider reports usage outside a checked bound.
    blocked = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class AIUsageReservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('core.Organization', on_delete=models.PROTECT)
    session_id = models.UUIDField()
    job_id = models.UUIDField()
    attempt = models.PositiveIntegerField()
    call_key = models.CharField(max_length=160)
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=200)
    unit = models.CharField(max_length=16)
    pricing_version = models.CharField(max_length=100)
    pricing_snapshot = models.JSONField(default=dict)
    max_units = models.PositiveBigIntegerField()
    actual_units = models.PositiveBigIntegerField(null=True)
    state = models.CharField(max_length=16, default='reserved')
    account_keys = models.JSONField(default=list)
    usage = models.JSONField(default=dict)
    provider_request_id = models.CharField(max_length=200, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True)
    settled_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['job_id', 'attempt', 'call_key'], name='ai_budget_unique_call_attempt'),
            models.CheckConstraint(condition=Q(state__in=['reserved', 'dispatched', 'settled', 'unknown', 'released']), name='ai_budget_reservation_state'),
            models.CheckConstraint(condition=Q(unit__in=['usd_micros', 'calls']), name='ai_budget_reservation_unit'),
        ]
        indexes = [models.Index(fields=['job_id', 'call_key'])]
