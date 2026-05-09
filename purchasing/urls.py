from django.urls import path
from . import views

app_name = 'purchasing'

urlpatterns = [
    path('suppliers/',              views.suppliers,           name='suppliers'),
    path('purchase-orders/',        views.purchase_orders,     name='purchase_orders'),
    path('purchase-orders/create/', views.create_purchase_order, name='create_po'),
    path('purchase-orders/<int:pk>/', views.po_detail,         name='po_detail'),
    path('invoices/',               views.invoices,            name='invoices'),
    path('purchase-orders/<int:pk>/delete/', views.delete_po, name='delete_po'),
]
