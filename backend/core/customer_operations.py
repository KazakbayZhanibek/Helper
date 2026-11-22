from django.conf import settings
from django.db import transaction
from django.db.models import Count, OuterRef, Prefetch, Q, Subquery
from rest_framework.response import Response
from rest_framework.views import APIView

from ai_drafts.providers import is_free_openrouter_model
from billing.services import preferred_subscriptions, snapshot
from customer_accounts.services import require_verified_email
from . import services as s
from .models import Membership, Organization


def kitchen_ai_options(org):
    available = bool(settings.EXTERNAL_AI_ENABLED and settings.STEP_DRAFT_BACKEND == 'openrouter'
                     and settings.STEP_DRAFT_ENDPOINT.rstrip('/') == 'https://openrouter.ai/api/v1'
                     and settings.OPENROUTER_API_KEY and is_free_openrouter_model(settings.STEP_DRAFT_MODEL))
    return {'enabled': org.external_ai_enabled, 'can_enable': available}


class KitchenAIView(APIView):
    def get(self, request, org_id):
        member = s.membership(request.user, org_id, {'admin'})
        return Response(kitchen_ai_options(member.organization))

    @transaction.atomic
    def post(self, request, org_id):
        member = s.membership(request.user, org_id, {'admin'})
        s.allowed_fields(request.data, {'enabled'})
        enabled = request.data.get('enabled')
        if type(enabled) is not bool:
            s.fail('VALIDATION_ERROR', 'Выберите режим обработки.')
        org = Organization.objects.select_for_update().get(pk=org_id)
        if enabled:
            require_verified_email(request.user)
            if not kitchen_ai_options(org)['can_enable']:
                s.fail('AI_UNAVAILABLE', 'Для кухни пока не настроена бесплатная модель.', 409)
        if org.external_ai_enabled != enabled:
            org.external_ai_enabled = enabled
            org.save(update_fields=['external_ai_enabled'])
            s.audit(member, 'organization.external_ai_enabled' if enabled else 'organization.external_ai_disabled', org)
        return Response(kitchen_ai_options(org))


class CustomersView(APIView):
    def get(self, request):
        if not request.user.is_superuser:
            s.fail('FORBIDDEN', 'Раздел доступен оператору сервиса.', 403)
        try:
            offset = int(request.query_params.get('offset', 0))
            if offset < 0 or offset > 10000000:
                raise ValueError
        except ValueError:
            s.fail('VALIDATION_ERROR', 'Некорректная страница.')
        subscription = Prefetch('billing_checkouts', queryset=preferred_subscriptions()[:1], to_attr='current_billing')
        query = Organization.objects.prefetch_related(subscription).order_by('-created_at', 'pk')
        term = request.query_params.get('q', '').strip()[:120]
        if term:
            admin_matches = Membership.objects.filter(role='admin', is_active=True, user__is_active=True, user__email__icontains=term).values('organization_id')
            query = query.filter(Q(name__icontains=term) | Q(pk__in=admin_matches))
        admins = Membership.objects.filter(organization_id=OuterRef('pk'),
                                           role='admin', is_active=True, user__is_active=True).order_by('joined_at', 'pk')
        rows = list(query.annotate(admin_email=Subquery(admins.values('user__email')[:1]),
                                   active_members=Count('membership', filter=Q(membership__is_active=True, membership__user__is_active=True), distinct=True)
                                   )[offset:offset + 26])
        items = []
        for org in rows[:25]:
            billing = snapshot(org.current_billing[0] if org.current_billing else None)
            items.append({'id': str(org.pk), 'name': org.name, 'email': org.admin_email,
                          'members': org.active_members, 'created_at': org.created_at,
                          'plan': billing['plan'], 'subscription_status': billing['status'], 'status': org.status})
        return Response({'items': items, 'next_offset': offset + 25 if len(rows) > 25 else None})
