from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView


class IsCompanyAdmin(BasePermission):
    """The user is a company admin. Scoping to their company is up to the view."""

    message = 'Only company admins can do this.'

    def has_permission(self, request: Request, view: APIView) -> bool:
        return hasattr(request.user, 'company_admin')
