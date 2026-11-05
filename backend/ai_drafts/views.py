from rest_framework.response import Response
from rest_framework.views import APIView

from core import services as core
from core.views import working_response
from . import services
from .providers import capabilities


class CapabilitiesView(APIView):
    def get(self, request, org_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        return Response(capabilities(member.organization))


class TranscriptView(APIView):
    def get(self, request, org_id, asset_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        return Response(services.transcript_data(services.transcript_for_asset(member, asset_id)))


class TranscribeView(APIView):
    def post(self, request, org_id, instruction_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        value, status = core.idempotent(request, member, lambda: services.request_transcription(member, instruction_id, request.data, request.headers.get('If-Match')))
        return Response(value, status=status)


class ProposalsView(APIView):
    def get(self, request, org_id, instruction_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        instruction = core.instruction_for(member, instruction_id)
        proposals = instruction.draft_proposals.select_related('transcript').filter(organization=member.organization, transcript__asset_id=instruction.source_asset_id)[:20]
        return Response({'items': [services.proposal_data(proposal, instruction) for proposal in proposals]})

    def post(self, request, org_id, instruction_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        value, status = core.idempotent(request, member, lambda: services.request_proposal(member, instruction_id, request.data, request.headers.get('If-Match')))
        return Response(value, status=status)


class ProposalView(APIView):
    def get(self, request, org_id, instruction_id, proposal_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        instruction, proposal = services.proposal_for(member, instruction_id, proposal_id)
        return Response(services.proposal_data(proposal, instruction))


class ApplyProposalView(APIView):
    def post(self, request, org_id, instruction_id, proposal_id):
        member = core.membership(request.user, org_id, core.WRITERS)
        value, status = core.idempotent(request, member, lambda: services.apply_proposal(member, instruction_id, proposal_id, request.data, request.headers.get('If-Match')))
        return working_response(value, status)
