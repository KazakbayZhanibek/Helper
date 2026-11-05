import uuid
from django.http import JsonResponse
from django.core.exceptions import RequestDataTooBig
from rest_framework.authentication import SessionAuthentication as DRFSessionAuthentication
from rest_framework.exceptions import APIException


class DomainError(APIException):
    def __init__(self, code, message, status=400, fields=None, details=None):
        self.status_code = status
        self.code = code
        self.message = message
        self.fields = fields or {}
        self.extra = details or {}
        super().__init__(message)


def error_body(request, code, message, fields=None, details=None):
    return {'error': {'code': code, 'message': message, 'field_errors': fields or {}, 'details': details or {}, 'request_id': getattr(request, 'request_id', ''), 'retryable': False}}


class SessionAuthentication(DRFSessionAuthentication):
    def authenticate_header(self, request):
        return 'Session'

    def enforce_csrf(self, request):
        # Django's checker reads request.POST before the CSRF header. Passing
        # DRF's wrapper would invoke content parsers for raw binary uploads.
        # The original HttpRequest keeps the same cookie/origin/header checks
        # without parsing or buffering a video body.
        return super().enforce_csrf(request._request)


def exception_handler(exc, context):
    from rest_framework.views import exception_handler as drf_exception_handler
    request = context['request']
    if isinstance(exc, DomainError):
        return __import__('rest_framework.response', fromlist=['Response']).Response(error_body(request, exc.code, exc.message, exc.fields, exc.extra), status=exc.status_code)
    if isinstance(exc, RequestDataTooBig):
        return __import__('rest_framework.response', fromlist=['Response']).Response(error_body(request, 'BODY_TOO_LARGE', 'Запрос превышает допустимый размер.'), status=413)
    response = drf_exception_handler(exc, context)
    if response is not None:
        status = response.status_code
        code = {401: 'AUTH_REQUIRED', 403: 'FORBIDDEN', 404: 'NOT_FOUND', 405: 'METHOD_NOT_ALLOWED', 400: 'VALIDATION_ERROR'}.get(status, 'REQUEST_FAILED')
        message = {401: 'Войдите в свой аккаунт.', 403: 'Недостаточно прав для этого действия.', 404: 'Материал не найден.', 405: 'Метод не поддерживается.', 400: 'Проверьте данные запроса.'}.get(status, 'Не удалось выполнить запрос.')
        if status == 403 and 'CSRF' in str(exc):
            code, message = 'CSRF_FAILED', 'Обновите страницу и повторите действие.'
        response.data = error_body(request, code, message)
    return response


def csrf_failure(request, reason=''):
    return JsonResponse(error_body(request, 'CSRF_FAILED', 'Обновите страницу и повторите действие.'), status=403)


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    def __call__(self, request):
        request.request_id = str(uuid.uuid4())
        response = self.get_response(request)
        response['X-Request-ID'] = request.request_id
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
