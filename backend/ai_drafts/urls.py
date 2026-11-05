from django.urls import path
from . import views

urlpatterns = [
    path('ai-capabilities', views.CapabilitiesView.as_view()),
    path('assets/<uuid:asset_id>/transcript', views.TranscriptView.as_view()),
    path('instructions/<uuid:instruction_id>/transcribe', views.TranscribeView.as_view()),
    path('instructions/<uuid:instruction_id>/draft-proposals', views.ProposalsView.as_view()),
    path('instructions/<uuid:instruction_id>/draft-proposals/<uuid:proposal_id>', views.ProposalView.as_view()),
    path('instructions/<uuid:instruction_id>/draft-proposals/<uuid:proposal_id>/apply', views.ApplyProposalView.as_view()),
]
