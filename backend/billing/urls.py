from django.urls import path
from rest_framework.routers import SimpleRouter

from billing import views

router = SimpleRouter()
router.register('invoices', views.InvoiceViewSet, basename='invoice')

urlpatterns = [
    path('billing/runs/', views.BillingRunView.as_view(), name='billing-run'),
    *router.urls,
]
