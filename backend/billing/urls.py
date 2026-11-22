from django.urls import path
from . import views


urlpatterns = [
    path('billing/plans', views.PlansView.as_view()),
    path('billing/webhooks/dodo', views.WebhookView.as_view()),
    path('orgs/<uuid:org_id>/billing', views.BillingView.as_view()),
    path('orgs/<uuid:org_id>/billing/checkout', views.CheckoutView.as_view()),
    path('orgs/<uuid:org_id>/billing/portal', views.PortalView.as_view()),
    path('orgs/<uuid:org_id>/billing/refresh', views.RefreshView.as_view()),
]
