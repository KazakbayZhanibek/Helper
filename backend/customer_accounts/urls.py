from django.urls import path, re_path

from . import views

urlpatterns = [
    path('auth/register', views.Register.as_view()),
    path('auth/password/forgot', views.Forgot.as_view()),
    path('auth/password/reset', views.Reset.as_view()),
    path('auth/email/verify', views.Verify.as_view()),
    path('auth/email/resend', views.Resend.as_view()),
    re_path(r'^auth/invitations/(?P<token>[a-zA-Z0-9._-]{1,160})$', views.AcceptInvitation.as_view()),
    path('orgs/<uuid:org_id>/team', views.Team.as_view()),
    path('orgs/<uuid:org_id>/team/<uuid:member_id>', views.RemoveMember.as_view()),
    path('orgs/<uuid:org_id>/invitations', views.Invitations.as_view()),
    path('orgs/<uuid:org_id>/invitations/<uuid:invite_id>', views.Revoke.as_view()),
]
