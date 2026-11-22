"""Only signed provider events can populate subscription state."""
import uuid
from django.conf import settings
from django.db import models


class LegacyLibrary(models.Model):
    """General-purpose documents already present at rollout keep a free archive."""
    organization = models.OneToOneField('core.Organization', primary_key=True, on_delete=models.CASCADE)
    upgraded_at = models.DateTimeField()


class Checkout(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('core.Organization', on_delete=models.PROTECT, related_name='billing_checkouts')
    payer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    mode = models.CharField(max_length=4)
    product_id = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    session_id = models.CharField(max_length=128)
    checkout_url = models.URLField(max_length=2048)
    subscription_id = models.CharField(max_length=128, null=True)
    customer_id = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=20, blank=True)
    period_end = models.DateTimeField(null=True)
    cancel_at_period_end = models.BooleanField(default=False)
    event_time = models.DateTimeField(null=True)
    last_checked_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['mode', 'subscription_id'], name='billing_mode_subscription')]
        indexes = [models.Index(fields=['organization', 'mode', 'created_at'], name='billing_kitchen_mode_time')]


class WebhookReceipt(models.Model):
    mode = models.CharField(max_length=4)
    event_id = models.CharField(max_length=128)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['mode', 'event_id'], name='billing_mode_event')]
