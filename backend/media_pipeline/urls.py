from django.urls import path
from . import views

urlpatterns = [
    path('instructions/<uuid:instruction_id>/uploads', views.UploadsView.as_view()),
    path('uploads/<uuid:upload_id>/content', views.UploadContentView.as_view()),
    path('uploads/<uuid:upload_id>', views.UploadView.as_view()),
    path('instructions/<uuid:instruction_id>/jobs', views.InstructionJobsView.as_view()),
    path('jobs/<uuid:job_id>', views.JobView.as_view()),
    path('jobs/<uuid:job_id>/cancel', views.CancelJobView.as_view()),
    path('jobs/<uuid:job_id>/retry', views.RetryJobView.as_view()),
]
