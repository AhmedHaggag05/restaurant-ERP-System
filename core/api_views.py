# pos_project/core/api_views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .permissions import IsManagerOrSuperadmin, IsCashierOrManager # <-- NEW IMPORT
from rest_framework import status
from django.utils import timezone
from django.db.models import Sum, Count, F
from decimal import Decimal

from .models import MenuItem, MenuCategory, Order, Ingredient
from .serializers import UserProfileSerializer, MenuCategorySerializer, MenuItemSerializer, OrderSerializer


class CurrentUserAPI(APIView):
    """GET /api/mobile/me/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data)


class POSMenuAPI(APIView):
    """GET /api/mobile/menu-items/ and categories"""
    permission_classes = [IsCashierOrManager]

    def get(self, request):
        categories = MenuCategory.objects.all().order_by('sort_order')
        items = MenuItem.objects.filter(is_active=True).select_related('category')

        return Response({
            'categories': MenuCategorySerializer(categories, many=True).data,
            'items': MenuItemSerializer(items, many=True).data
        })


class KitchenLiveOrdersAPI(APIView):
    """GET /api/mobile/live-orders/"""
    permission_classes = [IsCashierOrManager]

    def get(self, request):
        # Fetch active orders created today for the kitchen display
        today = timezone.now().date()
        orders = Order.objects.filter(
            status='active',
            created_at__date=today
        ).prefetch_related('items__menu_item').order_by('created_at')

        return Response(OrderSerializer(orders, many=True).data)


class ManagerDashboardAPI(APIView):
    """GET /api/mobile/dashboard-kpis/"""
    permission_classes = [IsManagerOrSuperadmin]

    def get(self, request):
        # Must be a manager or superadmin
        if not hasattr(request.user, 'profile') or request.user.profile.role not in ['manager', 'superadmin']:
            return Response({"error": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)

        today = timezone.now().date()

        # Highly optimized DB aggregations (No Python loops!)
        today_orders = Order.objects.filter(status='active', created_at__date=today)
        sales_data = today_orders.aggregate(
            total_revenue=Sum('total_price'),
            order_count=Count('id')
        )

        low_stock_count = Ingredient.objects.filter(stock_quantity__lte=F('low_stock_threshold')).count()

        return Response({
            "today_revenue": float(sales_data['total_revenue'] or 0),
            "today_orders": sales_data['order_count'],
            "low_stock_alerts": low_stock_count
        })

# Note: We will map the existing 'create_order' and 'validate_promo'
# from core.views directly in our urls to reuse the complex inventory logic safely.