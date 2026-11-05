from django.http import FileResponse
from rest_framework.response import Response
from rest_framework.views import APIView

from core import services as core
from core.views import working_response
from . import services as s
from .models import FrameEvidence, VisualProposal
from .provider import capabilities


class CapabilitiesView(APIView):
    def get(self, request, org_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        return Response(capabilities(member.organization))


class StartSessionView(APIView):
    def post(self, request, org_id, instruction_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        data, status = core.idempotent(request, member, lambda: s.start_live(member, instruction_id, request.data, request.headers.get('If-Match')))
        return Response(data, status=status)


class SessionView(APIView):
    def get(self, request, org_id, session_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        return Response(s.session_data(s.session_for(member, session_id)))


class FrameUploadView(APIView):
    parser_classes = []

    def post(self, request, org_id, session_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        if request.content_type != 'image/jpeg':
            core.fail('INVALID_FRAME_TYPE', 'Отправьте JPEG как image/jpeg.', 415)
        try:
            sequence = int(request.headers.get('X-Frame-Sequence', ''))
            timestamp = int(request.headers.get('X-Timestamp-Ms', ''))
        except (ValueError, TypeError):
            core.fail('VALIDATION_ERROR', 'Нужны X-Frame-Sequence и X-Timestamp-Ms.')
        data, status = s.receive_frame(member, session_id, request._request, sequence=sequence, timestamp_ms=timestamp, key=request.headers.get('Idempotency-Key'))
        return Response(data, status=status)


class FrameView(APIView):
    def get(self, request, org_id, frame_id):
        member = core.membership(request.user, org_id)
        frame = FrameEvidence.objects.select_related('session__instruction__current_version').filter(id=frame_id, organization=member.organization, session__organization=member.organization, session__instruction__lifecycle='active', session__frames_purged_at__isnull=True).first()
        if member.role == 'reader':
            # A published instruction grants access only to its frozen image links,
            # never to other frames from that session or to a newer working copy.
            version = frame.session.instruction.current_version if frame else None
            allowed = bool(frame and version and frame.session.mode == 'upload'
                           and version.organization_id == member.organization_id
                           and version.instruction_id == frame.session.instruction_id
                           and version.source_asset_id == frame.session.asset_id
                           and version.snapshot.get('source', {}).get('id') == str(version.source_asset_id)
                           and any(
                               step.get('evidence_visual_session_id') == str(frame.session_id)
                               and str(frame.id) in step.get('evidence_frame_ids', [])
                               for step in version.snapshot.get('steps', [])
                           ))
            if not allowed:
                # Keep the same response for unknown and unpublished frame IDs.
                core.fail('FORBIDDEN', 'Кадр недоступен в опубликованной инструкции.', 403)
        if not frame:
            core.fail('NOT_FOUND', 'Кадр не найден.', 404)
        path = s.safe_frame_path(frame.storage_key)
        if not path.is_file():
            core.fail('NOT_FOUND', 'Кадр не найден.', 404)
        response = FileResponse(path.open('rb'), content_type='image/jpeg')
        response['Cache-Control'] = 'private, no-store'
        return response


class AnalyzeSessionView(APIView):
    def post(self, request, org_id, session_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        data, status = core.idempotent(request, member, lambda: s.analyze_live(member, session_id, request.data))
        return Response(data, status=status)


class CancelSessionView(APIView):
    def post(self, request, org_id, session_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        core.allowed_fields(request.data, set())
        return Response(s.cancel_session(member, session_id))


class UploadedProposalsView(APIView):
    def get(self, request, org_id, instruction_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        instruction = core.instruction_for(member, instruction_id)
        query = VisualProposal.objects.filter(organization=member.organization, session__instruction=instruction, session__mode='upload').select_related('session__instruction__working_copy', 'session__asset')[:20]
        return Response({'items': [s.proposal_data(p) for p in query]})

    def post(self, request, org_id, instruction_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        data, status = core.idempotent(request, member, lambda: s.request_uploaded(member, instruction_id, request.data, request.headers.get('If-Match')))
        return Response(data, status=status)


class ProposalView(APIView):
    def get(self, request, org_id, instruction_id, proposal_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        return Response(s.proposal_data(s.proposal_for(member, instruction_id, proposal_id)))


class ApplyProposalView(APIView):
    def post(self, request, org_id, instruction_id, proposal_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        data, status = core.idempotent(request, member, lambda: s.apply_proposal(member, instruction_id, proposal_id, request.data, request.headers.get('If-Match')))
        return working_response(data, status)
