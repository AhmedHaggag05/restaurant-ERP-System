# pos_project/core/permissions.py
from rest_framework.permissions import BasePermission

class IsManagerOrSuperadmin(BasePermission):
    """Allows access only to managers and superadmins."""
    message = "You do not have permission to access manager endpoints."

    def has_permission(self, request, view):
        return bool(
            request.user and
            request.user.is_authenticated and
            hasattr(request.user, 'profile') and
            request.user.profile.role in ['manager', 'superadmin']
        )

class IsCashierOrManager(BasePermission):
    """Allows access to all staff roles."""
    message = "You must be a registered employee to access the POS API."

    def has_permission(self, request, view):
        return bool(
            request.user and
            request.user.is_authenticated and
            hasattr(request.user, 'profile') and
            request.user.profile.role in ['cashier', 'manager', 'superadmin']
        )