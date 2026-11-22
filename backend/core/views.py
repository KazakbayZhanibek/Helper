import hashlib
import re
from pathlib import Path
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.core.cache import cache
from django.db.models import Count, Q
from django.http import StreamingHttpResponse, HttpResponse
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import Membership, WorkingCopy, MediaAsset
from . import services as s


def profile(user):
    from customer_accounts.services import is_email_verified
    return {'user': {'id': str(user.id), 'email': user.email, 'display_name': user.display_name,
                     'email_verified': is_email_verified(user), 'is_operator': user.is_superuser},
            'organizations': [{'id': str(m.organization_id), 'name': m.organization.name, 'role': m.role}
                              for m in Membership.objects.select_related('organization').filter(user=user, is_active=True, organization__status='active').order_by('joined_at')],
            'demo_mode': settings.DEMO_MODE}


class CsrfView(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        return Response({'csrf_token': get_token(request)})


@method_decorator(csrf_protect, name='dispatch')
class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    def post(self, request):
        s.allowed_fields(request.data, {'email', 'password'})
        email = request.data.get('email', '')
        password = request.data.get('password', '')
        if not isinstance(email, str) or not isinstance(password, str) or len(email) > 254 or len(password) > 4096:
            s.fail('VALIDATION_ERROR', 'Проверьте email и пароль.')
        email = email.strip().lower()
        key = 'login:' + hashlib.sha256((request.META.get('REMOTE_ADDR', '') + ':' + email).encode()).hexdigest()
        failures = cache.get(key, 0)
        if failures >= 5:
            s.fail('RATE_LIMITED', 'Слишком много попыток входа. Повторите через 15 минут.', 429)
        user = authenticate(request, email=email, password=password)
        if user is None:
            cache.set(key, failures + 1, timeout=900)
            s.fail('INVALID_CREDENTIALS', 'Неверный email или пароль.', 401)
        cache.delete(key)
        login(request, user)
        return Response(profile(user))


class MeView(APIView):
    def get(self, request):
        return Response(profile(request.user))


class LogoutView(APIView):
    def post(self, request):
        logout(request)
        return Response(status=204)


class InstructionsView(APIView):
    def get(self, request, org_id):
        member = s.membership(request.user, org_id)
        query = member.organization.instruction_set.filter(lifecycle='active')
        reader = member.role == 'reader'
        if reader:
            query = query.filter(current_version__isnull=False)

        closed_publication = Q(working_copy__is_open=False, current_version__isnull=False)

        def visible(published, working):
            if reader:
                return published
            return (closed_publication & published) | (~closed_publication & working)

        search = s.normalize_library_search(request.query_params.get('q', '').strip()[:200])
        if search:
            # Authors can still find a publication when it also has an open revision.
            matches = Q(published_search_text__contains=search)
            if not reader:
                matches |= Q(working_copy__library_search_text__contains=search)
            query = query.filter(matches)
        kind = request.query_params.get('kind')
        if kind:
            if kind not in ('recipe', 'legacy'):
                s.fail('VALIDATION_ERROR', 'Выберите рецепты или архив.')
            recipe = visible(Q(current_version__snapshot__has_key='recipe'), Q(working_copy__recipe__isnull=False))
            query = query.filter(recipe) if kind == 'recipe' else query.exclude(recipe)
        language = request.query_params.get('language')
        if language:
            query = query.filter(visible(Q(current_version__snapshot__language=language), Q(working_copy__language=language)))
        status = request.query_params.get('status')
        if status:
            if status == 'published':
                if not reader:
                    query = query.filter(closed_publication)
            elif reader:
                query = query.none()
            elif status == 'draft':
                query = query.filter(Q(working_copy__is_open=True, working_copy__workflow='draft') |
                                     Q(working_copy__is_open=False, current_version__isnull=True))
            else:
                query = query.filter(working_copy__is_open=True, working_copy__workflow=status)
        try:
            offset = int(request.query_params.get('cursor', '0'))
            page_size = min(100, max(1, int(request.query_params.get('page_size', '20'))))
            if offset < 0:
                raise ValueError()
        except ValueError:
            s.fail('VALIDATION_ERROR', 'Некорректный параметр страницы.')
        total = query.count()
        query = query.select_related('working_copy', 'current_version', 'owner', 'source_asset')
        if not reader:
            query = query.annotate(working_step_count=Count('working_copy__steps'))
        page = query.order_by('-updated_at', 'id')[offset:offset + page_size] if offset < total else ()
        return Response({'items': [s.summary(item, reader) for item in page], 'next_cursor': str(offset + page_size) if offset + page_size < total else None, 'total': total})
    def post(self, request, org_id):
        member = s.membership(request.user, org_id, s.WRITERS)
        data, status = s.idempotent(request, member, lambda: s.create_instruction(member, request.data))
        return working_response(data, status)


def working_response(data, status=200):
    response = Response(data, status=status)
    if isinstance(data, dict) and 'etag' in data:
        response['ETag'] = data['etag']
    return response


class InstructionView(APIView):
    def get(self, request, org_id, instruction_id):
        member = s.membership(request.user, org_id)
        instruction = s.instruction_for(member, instruction_id)
        return Response({**s.summary(instruction, member.role == 'reader'), 'publication': s.version_data(instruction.current_version), 'working_copy': None if member.role == 'reader' else s.working_data(instruction.working_copy, member)})


    def delete(self, request, org_id, instruction_id):
        member = s.membership(request.user, org_id, {'admin', 'author'})
        s.delete_recipe_draft(member, instruction_id, request.headers.get('If-Match'))
        return Response(status=204)


class WorkingCopyView(APIView):
    def get(self, request, org_id, instruction_id):
        member = s.membership(request.user, org_id, s.WRITERS)
        instruction = s.instruction_for(member, instruction_id)
        return working_response(s.working_data(instruction.working_copy, member))
    def put(self, request, org_id, instruction_id):
        member = s.membership(request.user, org_id, s.WRITERS)
        return working_response(s.save_working(member, instruction_id, request.data, request.headers.get('If-Match')))


class WorkflowView(APIView):
    def post(self, request, org_id, instruction_id, action):
        roles = s.REVIEWERS if action in {'return-to-draft', 'review/check-step', 'publish', 'withdraw-publication'} else s.WRITERS
        member = s.membership(request.user, org_id, roles)
        if action == 'publish':
            data, status = s.idempotent(request, member, lambda: s.publish(member, instruction_id, request.data, request.headers.get('If-Match')))
            return Response(data, status=status)
        if action == 'create-draft':
            return working_response(s.create_draft(member, instruction_id, request.data), 201)
        if action == 'withdraw-publication':
            return Response(s.withdraw_publication(member, instruction_id, request.data))
        return working_response(s.workflow_action(member, instruction_id, action, request.data, request.headers.get('If-Match')))


class VersionsView(APIView):
    def get(self, request, org_id, instruction_id, version_id=None):
        member = s.membership(request.user, org_id, s.WRITERS)
        instruction = s.instruction_for(member, instruction_id)
        if version_id:
            version = instruction.versions.filter(id=version_id, organization_id=org_id).first()
            if version is None:
                s.fail('NOT_FOUND', 'Версия не найдена.', 404)
            return Response(s.version_data(version))
        return Response({'items': [s.version_data(v) for v in instruction.versions.select_related('published_by').order_by('-version_number')]})


class PlaybackView(APIView):
    def get(self, request, org_id, asset_id, derivative='playback'):
        member = s.membership(request.user, org_id)
        asset = MediaAsset.objects.filter(id=asset_id, organization_id=org_id, status='ready').first()
        if not asset:
            s.fail('NOT_FOUND', 'Видео не найдено.', 404)
        instruction = member.organization.instruction_set.filter(source_asset=asset, lifecycle='active').first()
        if not instruction or (member.role == 'reader' and (not instruction.current_version_id or instruction.current_version.source_asset_id != asset.id)):
            s.fail('NOT_FOUND', 'Видео не найдено.', 404)
        # No arbitrary client path or URL; storage key comes only from protected DB.
        root = settings.PRIVATE_MEDIA_ROOT.resolve()
        key = asset.thumbnail_key if derivative == 'thumbnail' else asset.storage_key
        path = (root / key).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            s.fail('NOT_FOUND', 'Видео не найдено.', 404)
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        byte_range = request.headers.get('Range')
        if byte_range:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', byte_range)
            invalid = not match or not any(match.groups()) or any(len(part) > 20 for part in match.groups())
            if not invalid:
                left, right = match.groups()
                if left:
                    start, end = int(left), min(int(right), size - 1) if right else size - 1
                else:
                    suffix = int(right)
                    invalid = suffix <= 0
                    start, end = max(0, size - suffix), size - 1
                invalid = invalid or start >= size or start > end
            if invalid:
                response = HttpResponse(status=416)
                response['Content-Range'] = f'bytes */{size}'
                return response
            status = 206
        length = max(0, end - start + 1)
        def chunks():
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block
        response = StreamingHttpResponse(chunks() if request.method != 'HEAD' else (), status=status, content_type='image/jpeg' if derivative == 'thumbnail' else 'video/mp4')
        response['Accept-Ranges'] = 'bytes'
        response['Content-Length'] = length
        response['Content-Disposition'] = 'inline'
        if status == 206:
            response['Content-Range'] = f'bytes {start}-{end}/{size}'
        return response


class HealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    def get(self, request):
        return Response({'status': 'ok'})
