from rest_framework.response import Response
from rest_framework.views import APIView

from core import services as s
from . import uploads, jobs
from .models import ProcessingJob


class UploadsView(APIView):
    def post(self, request, org_id, instruction_id):
        member = s.membership(request.user, org_id, s.WRITERS)
        data, status = s.idempotent(request, member, lambda: uploads.begin_upload(member, instruction_id, request.data))
        return Response(data, status=status)


class UploadContentView(APIView):
    # Raw binary deliberately bypasses parsers, after SessionAuthentication checks CSRF.
    parser_classes = []

    def post(self, request, org_id, upload_id):
        member = s.membership(request.user, org_id, s.WRITERS)
        if request.content_type != 'application/octet-stream':
            s.fail('UNSUPPORTED_MEDIA_TYPE', 'Отправьте исходный файл как application/octet-stream.', 415)
        raw_length = request.META.get('CONTENT_LENGTH')
        try:
            length = int(raw_length) if raw_length else None
        except (TypeError, ValueError):
            s.fail('VALIDATION_ERROR', 'Некорректный размер запроса.')
        data, status = uploads.receive_upload(member, upload_id, request._request, length)
        return Response(data, status=status)


class UploadView(APIView):
    def delete(self, request, org_id, upload_id):
        member = s.membership(request.user, org_id, s.WRITERS)
        uploads.cancel_upload(member, upload_id)
        return Response(status=204)


class InstructionJobsView(APIView):
    def get(self, request, org_id, instruction_id):
        member = s.membership(request.user, org_id, s.WRITERS)
        instruction = s.instruction_for(member, instruction_id)
        query = ProcessingJob.objects.filter(organization=member.organization, instruction=instruction).select_related('asset').order_by('-created_at')[:100]
        return Response({'items': [jobs.job_data(job) for job in query]})


class JobView(APIView):
    def get(self, request, org_id, job_id):
        member = s.membership(request.user, org_id, s.WRITERS)
        return Response(jobs.job_data(jobs.job_for(member, job_id)))


class CancelJobView(APIView):
    def post(self, request, org_id, job_id):
        member = s.membership(request.user, org_id, s.WRITERS)
        s.allowed_fields(request.data, set())
        return Response(jobs.job_data(jobs.cancel_job(member, job_id)), status=202)


class RetryJobView(APIView):
    def post(self, request, org_id, job_id):
        member = s.membership(request.user, org_id, s.WRITERS)
        s.allowed_fields(request.data, set())
        data, status = s.idempotent(request, member, lambda: (jobs.job_data(jobs.retry_job(member, job_id)), 202))
        return Response(data, status=status)
