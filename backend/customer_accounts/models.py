import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import Organization, User


class EmailIdentity(models.Model):
    """Tracks mailbox proof for self-service accounts; operator-provisioned users are trusted."""
    user = models.OneToOneField(User, primary_key=True, on_delete=models.CASCADE)
    email = models.EmailField(max_length=254)
    verified_at = models.DateTimeField(null=True)


class Invitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    email = models.EmailField(max_length=254)
    role = models.CharField(max_length=12)
    created_by = models.ForeignKey(User, related_name='+', on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)
    accepted_at = models.DateTimeField(null=True)
    accepted_by = models.ForeignKey(User, related_name='+', null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['organization', 'email'], condition=Q(revoked_at__isnull=True, accepted_at__isnull=True), name='kitchen_pending_invite_once'),
            models.CheckConstraint(condition=Q(role__in=['author', 'reviewer', 'reader']), name='kitchen_invite_valid_role'),
        ]


class AccountMail(models.Model):
    """Worker creates time-limited links immediately before delivery. No bearer secrets are stored."""
    kind = models.CharField(max_length=12)
    user = models.ForeignKey(User, null=True, on_delete=models.CASCADE)
    invitation = models.ForeignKey(Invitation, null=True, on_delete=models.CASCADE)
    identity_state = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    claim = models.UUIDField(null=True)


class AccountRateLimit(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    expires_at = models.DateTimeField(db_index=True)
    count = models.PositiveIntegerField(default=0)
