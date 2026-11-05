from django.urls import path
from . import views

urlpatterns = [
    path('visual-capabilities', views.CapabilitiesView.as_view()),
    path('instructions/<uuid:instruction_id>/visual-sessions', views.StartSessionView.as_view()),
    path('visual-sessions/<uuid:session_id>', views.SessionView.as_view()),
    path('visual-sessions/<uuid:session_id>/frames', views.FrameUploadView.as_view()),
    path('visual-sessions/<uuid:session_id>/analyze', views.AnalyzeSessionView.as_view()),
    path('visual-sessions/<uuid:session_id>/cancel', views.CancelSessionView.as_view()),
    path('visual-frames/<uuid:frame_id>', views.FrameView.as_view()),
    path('instructions/<uuid:instruction_id>/visual-proposals', views.UploadedProposalsView.as_view()),
    path('instructions/<uuid:instruction_id>/visual-proposals/<uuid:proposal_id>', views.ProposalView.as_view()),
    path('instructions/<uuid:instruction_id>/visual-proposals/<uuid:proposal_id>/apply', views.ApplyProposalView.as_view()),
]
