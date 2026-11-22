from django.urls import path
from .customer_operations import CustomersView, KitchenAIView

urlpatterns = [
    path('operator/customers', CustomersView.as_view()),
    path('orgs/<uuid:org_id>/ai-settings', KitchenAIView.as_view()),
]
