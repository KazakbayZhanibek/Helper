import uuid
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.core.exceptions import ValidationError


class UserManager(BaseUserManager):
    use_in_migrations = True
    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError('Email is required')
        user = self.model(email=email.strip().lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user
    def create_superuser(self, email, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True)
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=120)
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['display_name']
    objects = UserManager()
    class Meta:
        constraints = [models.UniqueConstraint(Lower('email'), name='user_email_ci_unique')]


class Organization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    status = models.CharField(max_length=12, default='active', choices=[(s, s) for s in ['active', 'suspended', 'deleted']])
    timezone = models.CharField(max_length=64, default='Asia/Almaty')
    external_ai_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class Membership(models.Model):
    ROLES = ['admin', 'author', 'reviewer', 'reader']
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=12, choices=[(r, r) for r in ROLES])
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'user'], name='unique_org_user'), models.CheckConstraint(condition=Q(role__in=['admin', 'author', 'reviewer', 'reader']), name='valid_membership_role')]


class TenantEntity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        abstract = True


class MediaAsset(TenantEntity):
    # Originals are quarantined; only prepared derivatives are served to clients.
    storage_key = models.CharField(max_length=200, default='', blank=True)
    original_key = models.CharField(max_length=200, default='', blank=True)
    original_size_bytes = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, default='', blank=True)
    audio_key = models.CharField(max_length=200, default='', blank=True)
    thumbnail_key = models.CharField(max_length=200, default='', blank=True)
    has_audio = models.BooleanField(default=False)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=12, default='ready', choices=[(s, s) for s in ['quarantined', 'validating', 'ready', 'rejected', 'deleted']])
    is_demo = models.BooleanField(default=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=Q(duration_ms__gt=0), name='asset_positive_duration')]


class Instruction(TenantEntity):
    owner = models.ForeignKey(User, on_delete=models.PROTECT)
    source_asset = models.OneToOneField(MediaAsset, null=True, blank=True, on_delete=models.PROTECT)
    lifecycle = models.CharField(max_length=12, default='active')
    current_version = models.ForeignKey('PublishedVersion', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    published_search_text = models.TextField(default='')


class WorkingCopy(TenantEntity):
    instruction = models.OneToOneField(Instruction, on_delete=models.CASCADE, related_name='working_copy')
    revision = models.PositiveIntegerField(default=1)
    is_open = models.BooleanField(default=True)
    workflow = models.CharField(max_length=12, default='draft', choices=[('draft', 'draft'), ('in_review', 'in_review')])
    title = models.CharField(max_length=120)
    purpose = models.CharField(max_length=600)
    language = models.CharField(max_length=5, default='ru', choices=[('ru', 'ru'), ('kk', 'kk'), ('mixed', 'mixed')])
    review_requested_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='+')
    review_requested_at = models.DateTimeField(null=True)
    based_on_version = models.ForeignKey('PublishedVersion', null=True, on_delete=models.PROTECT, related_name='+')
    return_reason = models.TextField(default='', blank=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=Q(revision__gte=1), name='working_positive_revision'), models.CheckConstraint(condition=Q(workflow__in=['draft', 'in_review']), name='working_valid_workflow')]


class WorkingStep(TenantEntity):
    working_copy = models.ForeignKey(WorkingCopy, on_delete=models.CASCADE, related_name='steps')
    ordinal = models.PositiveIntegerField()
    title = models.CharField(max_length=120)
    description = models.TextField()
    start_ms = models.PositiveIntegerField()
    end_ms = models.PositiveIntegerField()
    origin = models.CharField(max_length=12, default='manual')
    evidence_segment_ids = models.JSONField(default=list)
    evidence_transcript_revision = models.PositiveIntegerField(null=True, blank=True)
    evidence_frame_ids = models.JSONField(default=list)
    evidence_visual_session_id = models.UUIDField(null=True, blank=True)
    warning_codes = models.JSONField(default=list)
    checked_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    checked_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        ordering = ['ordinal']
        constraints = [models.UniqueConstraint(fields=['working_copy', 'ordinal'], name='unique_step_order'), models.CheckConstraint(condition=Q(end_ms__gt=models.F('start_ms')), name='valid_step_interval')]


class PublishedVersion(TenantEntity):
    instruction = models.ForeignKey(Instruction, on_delete=models.CASCADE, related_name='versions')
    version_number = models.PositiveIntegerField()
    source_asset = models.ForeignKey(MediaAsset, on_delete=models.PROTECT)
    snapshot = models.JSONField()
    content_sha256 = models.CharField(max_length=64)
    source_working_revision = models.PositiveIntegerField()
    published_by = models.ForeignKey(User, on_delete=models.PROTECT)
    published_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['instruction', 'version_number'], name='unique_version_number')]
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Published content is immutable; create a new draft.')
        return super().save(*args, **kwargs)


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, null=True, on_delete=models.SET_NULL)
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=64)
    object_id = models.UUIDField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict)


class IdempotencyRecord(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    actor = models.ForeignKey(User, on_delete=models.CASCADE)
    route = models.CharField(max_length=250)
    key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField()
    response_json = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'actor', 'route', 'key'], name='unique_idempotency_scope')]
