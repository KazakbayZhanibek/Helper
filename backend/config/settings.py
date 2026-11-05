"""Local development defaults. Production requires explicit env and PostgreSQL."""
import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv('DJANGO_DEBUG', '1') == '1'
DEMO_MODE = os.getenv('DEMO_MODE', '1' if DEBUG else '0') == '1'
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured('DJANGO_SECRET_KEY is required outside local development')
    SECRET_KEY = 'local-development-only-do-not-deploy-master-experience'
ALLOWED_HOSTS = os.getenv('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',')
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'rest_framework', 'core', 'media_pipeline', 'ai_drafts', 'visual_drafts', 'ai_budget']
MIDDLEWARE = ['core.http.RequestIdMiddleware', 'django.middleware.security.SecurityMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware']
ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / 'db.sqlite3', 'OPTIONS': {'timeout': 20}}}
if os.getenv('POSTGRES_HOST'):
    DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'HOST': os.environ['POSTGRES_HOST'], 'PORT': os.getenv('POSTGRES_PORT', '5432'), 'NAME': os.getenv('POSTGRES_DB', 'master_experience'), 'USER': os.getenv('POSTGRES_USER', 'master_experience'), 'PASSWORD': os.environ.get('POSTGRES_PASSWORD', '')}}
elif not DEBUG:
    raise ImproperlyConfigured('PostgreSQL is required outside local development')
if not DEBUG and DEMO_MODE:
    raise ImproperlyConfigured('Demo mode must be disabled outside local development')
AUTH_USER_MODEL = 'core.User'
AUTH_PASSWORD_VALIDATORS = [{'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}}, {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'}, {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'}]
LANGUAGE_CODE = 'ru'
TIME_ZONE = 'Asia/Almaty'
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 43200
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_TRUSTED_ORIGINS = os.getenv('CSRF_TRUSTED_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173').split(',')
CSRF_FAILURE_VIEW = 'core.http.csrf_failure'
DATA_UPLOAD_MAX_MEMORY_SIZE = 262144
REST_FRAMEWORK = {'DEFAULT_AUTHENTICATION_CLASSES': ['core.http.SessionAuthentication'], 'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'], 'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'], 'EXCEPTION_HANDLER': 'core.http.exception_handler', 'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser']}
PRIVATE_MEDIA_ROOT = Path(os.getenv('PRIVATE_MEDIA_ROOT', str(BASE_DIR / 'private-media'))).resolve()
DEMO_DURATION_MS = 60000
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
APPEND_SLASH = False

# No media subprocess or cloud call starts without an explicit operator config.
FFMPEG_PATH = os.getenv('FFMPEG_PATH', 'ffmpeg')
FFPROBE_PATH = os.getenv('FFPROBE_PATH', 'ffprobe')
# Local preparation only; all strategies retain the same 12-image AI allowance.
VIDEO_FRAME_STRATEGY = os.getenv('VIDEO_FRAME_STRATEGY', 'events')
# Experimental action evidence contract; new sessions only, pending quality trials.
VISUAL_ACTION_EVIDENCE = os.getenv('VISUAL_ACTION_EVIDENCE', '0') == '1'
# Optional second look, charged to the existing session limit; new uploads only.
VISUAL_SEMANTIC_REVIEW = os.getenv('VISUAL_SEMANTIC_REVIEW', '0') == '1'
# One-response temporal inventory; pinned for new upload sessions only.
VISUAL_SHORT_ACTION_COVERAGE = os.getenv('VISUAL_SHORT_ACTION_COVERAGE', '0') == '1'
# Same-call frame facts for new covered upload sessions; no automatic extra passes.
VISUAL_FRAME_GROUNDING = os.getenv('VISUAL_FRAME_GROUNDING', '0') == '1'
# Existing sessions keep their immutable version; opt into inline evidence for new ones.
VISUAL_FRAME_GROUNDING_VERSION = os.getenv('VISUAL_FRAME_GROUNDING_VERSION', 'v1')
ASR_BACKEND = os.getenv('ASR_BACKEND', '')
WHISPER_CPP_BINARY = os.getenv('WHISPER_CPP_BINARY', '')
ASR_MODEL_PATH = os.getenv('ASR_MODEL_PATH', '')
ASR_PYTHON = os.getenv('ASR_PYTHON', '')
STEP_DRAFT_BACKEND = os.getenv('STEP_DRAFT_BACKEND', '')
STEP_DRAFT_ENDPOINT = os.getenv('STEP_DRAFT_ENDPOINT', '')
STEP_DRAFT_MODEL = os.getenv('STEP_DRAFT_MODEL', '')
STEP_DRAFT_API_KEY = os.getenv('STEP_DRAFT_API_KEY', os.getenv('OPENAI_API_KEY', ''))
NVIDIA_API_KEY = os.getenv('NVIDIA_API_KEY', '')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '')
# Conservative application-wide free quota. Never raised above 50 by this adapter.
OPENROUTER_FREE_DAY_CALLS = int(os.getenv('OPENROUTER_FREE_DAY_CALLS', '50'))
# Lifetime spend + unresolved reservations across all sessions; paid mode off at 0.
OPENROUTER_TOTAL_BUDGET_MICROUSD = int(os.getenv('OPENROUTER_TOTAL_BUDGET_MICROUSD', '0'))
EXTERNAL_AI_ENABLED = os.getenv('EXTERNAL_AI_ENABLED', '0') == '1'
# Opt-in for one trusted developer's local subscription-based experiment only.
CODEX_TEST_MODE = os.getenv('CODEX_TEST_MODE', '0') == '1'
CODEX_CLI_BINARY = os.getenv('CODEX_CLI_BINARY', '')

# Per-call reservations are enforced before AI transport, separately from billing.
AI_BUDGET_ENABLED = os.getenv('AI_BUDGET_ENABLED', '1') == '1'
AI_BUDGET_DEFAULT_CALLS = int(os.getenv('AI_BUDGET_DEFAULT_CALLS', '30'))
AI_BUDGET_MAX_CALLS = int(os.getenv('AI_BUDGET_MAX_CALLS', '60'))
AI_BUDGET_ORG_DAY_CALLS = int(os.getenv('AI_BUDGET_ORG_DAY_CALLS', '100'))
AI_BUDGET_PLATFORM_DAY_CALLS = int(os.getenv('AI_BUDGET_PLATFORM_DAY_CALLS', '500'))
AI_BUDGET_DEFAULT_MICROUSD = int(os.getenv('AI_BUDGET_DEFAULT_MICROUSD', '2000000'))
AI_BUDGET_MAX_MICROUSD = int(os.getenv('AI_BUDGET_MAX_MICROUSD', '5000000'))
AI_BUDGET_ORG_DAY_MICROUSD = int(os.getenv('AI_BUDGET_ORG_DAY_MICROUSD', '10000000'))
AI_BUDGET_PLATFORM_DAY_MICROUSD = int(os.getenv('AI_BUDGET_PLATFORM_DAY_MICROUSD', '50000000'))
AI_BUDGET_ORG_MONTH_CALLS = int(os.getenv('AI_BUDGET_ORG_MONTH_CALLS', '2000'))
AI_BUDGET_ORG_MONTH_MICROUSD = int(os.getenv('AI_BUDGET_ORG_MONTH_MICROUSD', '100000000'))
