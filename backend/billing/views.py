from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsCompanyAdmin
from billing.models import Invoice
from billing.serializers import (
    BillingRunRequestSerializer,
    BillingRunSerializer,
    InvoiceDetailSerializer,
    InvoiceListQuerySerializer,
    InvoiceSummarySerializer,
)
from billing.services import run_billing


class BillingRunView(APIView):
    permission_classes = [IsAuthenticated, IsCompanyAdmin]

    def post(self, request: Request) -> Response:
        body = BillingRunRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        period = body.validated_data['month']

        run = run_billing(
            company=request.user.company_admin.company, triggered_by=request.user, period=period
        )
        return Response(BillingRunSerializer(run).data, status=status.HTTP_201_CREATED)


class InvoiceViewSet(viewsets.ViewSet):
    """Invoices for the admin's company."""

    permission_classes = [IsAuthenticated, IsCompanyAdmin]
    lookup_value_regex = r'\d+'

    def get_queryset(self) -> QuerySet[Invoice]:
        return Invoice.objects.filter(
            company_id=self.request.user.company_admin.company_id
        ).prefetch_related('deliveries')

    def list(self, request: Request) -> Response:
        """GET /api/invoices/?month=YYYY-MM (month optional)."""
        query = InvoiceListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        invoices = self.get_queryset()
        if 'month' in query.validated_data:
            invoices = invoices.filter(period=query.validated_data['month'])
        return Response(InvoiceSummarySerializer(invoices, many=True).data)

    def retrieve(self, request: Request, pk: str) -> Response:
        """GET /api/invoices/<id>/ with lines and deliveries."""
        invoice = get_object_or_404(self.get_queryset().prefetch_related('lines'), pk=pk)
        return Response(InvoiceDetailSerializer(invoice).data)
