# pos_project/core/api_urls.py
from django.urls import path
from . import api_views
from . import views  # Import existing views to reuse the JSON cart logic

urlpatterns = [
    # Auth & Profile
    path('me/', api_views.CurrentUserAPI.as_view(), name='api_me'),

    # POS
    path('menu/', api_views.POSMenuAPI.as_view(), name='api_menu'),
    path('create-order/', views.create_order, name='api_create_order'),  # Reusing existing robust JSON view
    path('validate-promo/', views.validate_promo, name='api_validate_promo'),  # Reusing existing JSON view

    # Kitchen Display
    path('live-orders/', api_views.KitchenLiveOrdersAPI.as_view(), name='api_live_orders'),

    # Manager
    path('dashboard-kpis/', api_views.ManagerDashboardAPI.as_view(), name='api_dashboard_kpis'),
]