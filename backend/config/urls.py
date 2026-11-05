from django.urls import include, path, re_path
from core import views

urlpatterns = [
    path('api/v1/auth/csrf', views.CsrfView.as_view()),
    path('api/v1/auth/login', views.LoginView.as_view()),
    path('api/v1/auth/me', views.MeView.as_view()),
    path('api/v1/auth/logout', views.LogoutView.as_view()),
    path('api/v1/health/live', views.HealthView.as_view()),
    path('health/live', views.HealthView.as_view()),
    path('api/v1/orgs/<uuid:org_id>/instructions', views.InstructionsView.as_view()),
    path('api/v1/orgs/<uuid:org_id>/instructions/<uuid:instruction_id>', views.InstructionView.as_view()),
    path('api/v1/orgs/<uuid:org_id>/instructions/<uuid:instruction_id>/working-copy', views.WorkingCopyView.as_view()),
    path('api/v1/orgs/<uuid:org_id>/instructions/<uuid:instruction_id>/versions', views.VersionsView.as_view()),
    path('api/v1/orgs/<uuid:org_id>/instructions/<uuid:instruction_id>/versions/<uuid:version_id>', views.VersionsView.as_view()),
    path('api/v1/orgs/<uuid:org_id>/assets/<uuid:asset_id>/playback', views.PlaybackView.as_view()),
    path('api/v1/orgs/<uuid:org_id>/assets/<uuid:asset_id>/thumbnail', views.PlaybackView.as_view(), {'derivative': 'thumbnail'}),
    path('api/v1/orgs/<uuid:org_id>/', include('media_pipeline.urls')),
    path('api/v1/orgs/<uuid:org_id>/', include('ai_drafts.urls')),
    path('api/v1/orgs/<uuid:org_id>/', include('visual_drafts.urls')),
]
for action in ['submit-review', 'withdraw-review', 'return-to-draft', 'review/check-step', 'publish', 'create-draft', 'withdraw-publication']:
    urlpatterns.append(path(f'api/v1/orgs/<uuid:org_id>/instructions/<uuid:instruction_id>/{action}', views.WorkflowView.as_view(), {'action': action}))
