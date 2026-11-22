import json

from django.core.exceptions import RequestDataTooBig
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core import services as core
from . import provider, services


class PlansView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({'plans': services.plans(provider.offer())})


class BillingView(APIView):
    def get(self, request, org_id):
        return Response(services.status(core.membership(request.user, org_id)))


class CheckoutView(APIView):
    def post(self, request, org_id):
        member = core.membership(request.user, org_id, {'admin'})
        core.allowed_fields(request.data, set())
        return Response({'url': services.checkout(member)})


class PortalView(APIView):
    def post(self, request, org_id):
        member = core.membership(request.user, org_id, {'admin'})
        core.allowed_fields(request.data, set())
        return Response({'url': services.portal(member)})


class RefreshView(APIView):
    def post(self, request, org_id):
        member = core.membership(request.user, org_id, {'admin'})
        core.allowed_fields(request.data, set())
        return Response(services.refresh(member))


class WebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    parser_classes = []

    def post(self, request):
        try:
            body = request._request.body
        except RequestDataTooBig:
            core.fail('PAYLOAD_TOO_LARGE', 'Уведомление слишком велико.', 413)
        if len(body) > 131072:
            core.fail('PAYLOAD_TOO_LARGE', 'Уведомление слишком велико.', 413)
        event_id = provider.verify(body, request.headers)
        try:
            event = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            core.fail('INVALID_BILLING_EVENT', 'Ожидается JSON.', 400)
        services.process_event(event_id, event)
        return Response(status=204)
