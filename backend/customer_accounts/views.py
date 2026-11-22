from datetime import timedelta

from django.contrib.auth import login
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core import services as core
from core.models import Membership, Organization, User
from core.views import profile
from . import services as s
from .models import AccountMail, EmailIdentity, Invitation


@method_decorator(csrf_protect, name='dispatch')
class PublicView(APIView):
    permission_classes = [AllowAny]


class Register(PublicView):
    def post(self, request):
        s.rate_limit(request, 'register', 15, 15)
        core.allowed_fields(request.data, {'email', 'password', 'display_name', 'kitchen_name'})
        if request.user.is_authenticated:
            core.fail('ALREADY_SIGNED_IN', 'Сначала выйдите из текущего аккаунта.', 409)
        email = s.email_field(request.data)
        kitchen = s.name_field(request.data, 'kitchen_name')
        with transaction.atomic():
            user = s.create_user(email, request.data)
            org = Organization.objects.create(name=kitchen)
            admin = Membership.objects.create(organization=org, user=user, role='admin')
            core.audit(admin, 'kitchen.created', org)
        login(request, user)
        return Response(profile(user), status=201)


class Forgot(PublicView):
    def post(self, request):
        s.rate_limit(request, 'forgot', 30, 15)
        core.allowed_fields(request.data, {'email'})
        email = s.email_field(request.data)
        if s.allowance('forgot-email', email, 3, 60):
            user = User.objects.filter(email__iexact=email, is_active=True).first()
            if user and user.has_usable_password():
                AccountMail.objects.create(kind='reset', user=user, identity_state=s.reset_state(user))
        return Response({'ok': True}, status=202)


class Reset(PublicView):
    def post(self, request):
        s.rate_limit(request, 'reset', 30, 15)
        core.allowed_fields(request.data, {'uid', 'token', 'password'})
        with transaction.atomic():
            user = s.link_user(request.data)
            if not user or not default_token_generator.check_token(user, request.data['token']):
                core.fail('LINK_INVALID', 'Ссылка истекла. Запросите новое письмо.', 410)
            user.set_password(s.password_field(request.data, user))
            user.save(update_fields=['password'])
            # Possession of this email-only token also proves ownership of the mailbox.
            EmailIdentity.objects.filter(user=user).update(email=user.email, verified_at=timezone.now())
        return Response({'ok': True})


class Verify(PublicView):
    def post(self, request):
        s.rate_limit(request, 'verify', 40, 15)
        core.allowed_fields(request.data, {'uid', 'token'})
        with transaction.atomic():
            user = s.link_user(request.data)
            try:
                valid = user and constant_time_compare(s.verified_state(user), signing.TimestampSigner(
                    salt='customer_accounts.confirm.v1').unsign(request.data['token'], max_age=86400))
            except signing.BadSignature:
                valid = False
            if not valid:
                core.fail('LINK_INVALID', 'Ссылка истекла. Запросите новое письмо подтверждения.', 410)
            EmailIdentity.objects.filter(user=user).update(email=user.email, verified_at=timezone.now())
        return Response({'ok': True})


class Resend(APIView):
    def post(self, request):
        core.allowed_fields(request.data, set())
        if not s.is_email_verified(request.user):
            if not s.allowance('verify-user', str(request.user.pk), 4, 60):
                core.fail('RATE_LIMITED', 'Письмо уже отправлено. Попробуйте позже.', 429)
            s.queue_verification(request.user)
        return Response({'ok': True}, status=202)


class Team(APIView):
    def get(self, request, org_id):
        member = core.membership(request.user, org_id, {'admin'})
        members = Membership.objects.select_related('user').filter(
            organization=member.organization, is_active=True, user__is_active=True).order_by('joined_at', 'pk')
        invites = Invitation.objects.filter(organization=member.organization, accepted_at__isnull=True,
                                            revoked_at__isnull=True, expires_at__gt=timezone.now()).order_by('-created_at')
        return Response({
            'members': [{'id': str(m.pk), 'email': m.user.email, 'display_name': m.user.display_name, 'role': m.role} for m in members],
            'invitations': [s.invite_details(invite) for invite in invites],
        })


class RemoveMember(APIView):
    @transaction.atomic
    def delete(self, request, org_id, member_id):
        admin = s.locked_admin(request.user, org_id)
        target = Membership.objects.select_for_update().filter(organization=admin.organization, pk=member_id, is_active=True).first()
        if not target:
            core.fail('NOT_FOUND', 'Участник не найден.', 404)
        if target.user_id == request.user.pk or target.role == 'admin':
            core.fail('ADMIN_PROTECTED', 'Администратора кухни нельзя удалить через список участников.', 409)
        target.is_active = False
        target.save(update_fields=['is_active'])
        core.audit(admin, 'kitchen.member_removed', target)
        return Response(status=204)


class Invitations(APIView):
    def post(self, request, org_id):
        from billing.services import enforce_member_limit

        core.allowed_fields(request.data, {'email', 'role'})
        email = s.email_field(request.data)
        role = request.data.get('role')
        if role not in ('reader', 'author', 'reviewer'):
            core.fail('VALIDATION_ERROR', 'Выберите роль для приглашения.', fields={'role': ['Выберите роль из списка.']})
        with transaction.atomic():
            member = s.locked_admin(request.user, org_id)
            s.require_verified_email(request.user)
            if Membership.objects.filter(organization_id=org_id, user__email__iexact=email, is_active=True).exists():
                core.fail('ALREADY_MEMBER', 'Этот пользователь уже на вашей кухне.', 409)
            now = timezone.now()
            pending = Invitation.objects.filter(organization_id=org_id, accepted_at__isnull=True, revoked_at__isnull=True)
            others = pending.filter(expires_at__gt=now).exclude(email=email).count()
            enforce_member_limit(member.organization, pending=others)
            if not s.allowance('invite-sender', str(member.pk), 20, 60):
                core.fail('RATE_LIMITED', 'Слишком много приглашений. Попробуйте позже.', 429)
            pending.filter(email=email).update(revoked_at=now)
            invite = Invitation.objects.create(organization_id=org_id, email=email, role=role, created_by=request.user,
                                               expires_at=now + timedelta(days=7))
            AccountMail.objects.create(kind='invite', invitation=invite)
            core.audit(member, 'kitchen.invited', invite)
        return Response({**s.invite_details(invite), 'invite_url': s.public_link('/invite/' + s.invite_token(invite))}, status=201)


class Revoke(APIView):
    @transaction.atomic
    def delete(self, request, org_id, invite_id):
        member = s.locked_admin(request.user, org_id)
        invite = Invitation.objects.filter(organization_id=org_id, pk=invite_id, accepted_at__isnull=True).first()
        if not invite:
            core.fail('NOT_FOUND', 'Активное приглашение не найдено.', 404)
        if not invite.revoked_at:
            invite.revoked_at = timezone.now()
            invite.save(update_fields=['revoked_at'])
            core.audit(member, 'kitchen.invite_revoked', invite)
        return Response(status=204)


class AcceptInvitation(PublicView):
    def get(self, request, token):
        invite, _ = s.get_invitation(token)
        if invite.accepted_at:
            core.fail('INVITATION_ACCEPTED', 'Приглашение уже принято.', 410)
        name, domain = invite.email.rsplit('@', 1)
        return Response({'organization_id': str(invite.organization_id), 'kitchen_name': invite.organization.name,
                         'email_hint': name[0] + '…@' + domain, 'role': invite.role,
                         'has_account': User.objects.filter(email__iexact=invite.email).exists(),
                         'matches_session': request.user.email.lower() == invite.email if request.user.is_authenticated else None})

    def post(self, request, token):
        from billing.services import enforce_member_limit

        s.rate_limit(request, 'accept', 30, 15)
        core.allowed_fields(request.data, {'display_name', 'password'})
        with transaction.atomic():
            invite, mailbox = s.get_invitation(token, lock=True)
            if invite.accepted_at:
                if invite.accepted_by_id == request.user.pk and request.user.is_authenticated:
                    return Response(profile(request.user))
                core.fail('INVITATION_ACCEPTED', 'Приглашение уже принято.', 410)
            existing = User.objects.filter(email__iexact=invite.email).first()
            if request.user.is_authenticated and (not existing or existing.pk != request.user.pk):
                core.fail('INVITATION_ACCOUNT_MISMATCH', 'Войдите в аккаунт, которому адресовано приглашение.', 403)
            if existing and not request.user.is_authenticated:
                core.fail('INVITATION_LOGIN_REQUIRED', 'Войдите в свой аккаунт, чтобы принять приглашение.', 409)
            current = Membership.objects.filter(organization=invite.organization, user=existing).first() if existing else None
            if not current or not current.is_active:
                enforce_member_limit(invite.organization)
            user = existing or s.create_user(invite.email, request.data, mailbox=mailbox)
            if existing and mailbox:
                EmailIdentity.objects.filter(user=existing).update(email=existing.email, verified_at=timezone.now())
            if not current:
                current = Membership.objects.create(organization=invite.organization, user=user, role=invite.role)
            elif not current.is_active:
                current.role, current.is_active = invite.role, True
                current.save(update_fields=['role', 'is_active'])
            invite.accepted_by = user
            invite.accepted_at = timezone.now()
            invite.save(update_fields=['accepted_by', 'accepted_at'])
            core.audit(current, 'kitchen.invite_accepted', invite)
        if not request.user.is_authenticated:
            login(request, user)
        return Response(profile(user))
