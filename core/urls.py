from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('pos/', views.pos_view, name='pos'),
    path('api/create-order/', views.create_order, name='create_order'),
    path('receipt/<str:order_number>/', views.receipt_view, name='receipt'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('sales-report/', views.sales_report, name='sales_report'),
    path('sales-report/pdf/financial/', views.pdf_report_financial, name='pdf_report_financial'),
    path('sales-report/pdf/ingredients/', views.pdf_report_ingredients, name='pdf_report_ingredients'),
    path('manage-users/', views.manage_users, name='manage_users'),
    path('delete-user/<int:user_id>/', views.delete_user, name='delete_user'),
    path('edit-user/<int:user_id>/', views.edit_user, name='edit_user'),
    path('manage-ingredients/', views.manage_ingredients, name='manage_ingredients'),
    path('manage-ingredients/page/', views.manage_ingredients_page, name='manage_ingredients_page'),
    path('manage-ingredients/adjust/', views.manual_stock_adjust, name='manual_stock_adjust'),
    path('manage-menu/', views.manage_menu, name='manage_menu'),
    path('edit-menu-item/<int:pk>/', views.edit_menu_item, name='edit_menu_item'),
    path('toggle-menu-item/<int:pk>/', views.toggle_menu_item_status, name='toggle_menu_item_status'),
    path('delete-ingredient/<int:ingredient_id>/', views.delete_ingredient, name='delete_ingredient'),
    path('archive-ingredient/<int:ingredient_id>/', views.archive_ingredient, name='archive_ingredient'),
    path('delete-menu-item/<int:pk>/', views.delete_menu_item, name='delete_menu_item'),
    path('delete-order/<int:order_id>/', views.delete_order, name='delete_order'),
    path('employees/', views.manage_employees, name='manage_employees'),
    path('employees/add/', views.add_employee, name='add_employee'),
    path('employees/edit/<int:employee_id>/', views.edit_employee, name='edit_employee'),
    path('employees/delete/<int:employee_id>/', views.delete_employee, name='delete_employee'),
    # Promo codes
    path('promos/', views.manage_promos, name='manage_promos'),
    path('promos/add/', views.add_promo, name='add_promo'),
    path('promos/edit/<int:promo_id>/', views.edit_promo, name='edit_promo'),
    path('promos/delete/<int:promo_id>/', views.delete_promo, name='delete_promo'),
    # Expenses
    path('expenses/', views.manage_expenses, name='manage_expenses'),
    path('expenses/add/', views.add_expense, name='add_expense'),
    path('expenses/edit/<int:expense_id>/', views.edit_expense, name='edit_expense'),
    path('expenses/delete/<int:expense_id>/', views.delete_expense, name='delete_expense'),
    # Shift report & promo validation
    path('shift-report/', views.shift_report, name='shift_report'),
    path('manage-orders/', views.manage_orders, name='manage_orders'),
    path('shift-report/pdf/', views.shift_report_pdf, name='shift_report_pdf'),
    path('api/validate-promo/', views.validate_promo, name='validate_promo'),
    # Void order
    path('void-order/<int:order_id>/', views.void_order, name='void_order'),
    path('edit-order/<int:order_id>/', views.edit_order, name='edit_order'),
    path('log-damage/<int:order_id>/', views.log_damage, name='log_damage'),
    path('damaged-stock/', views.damaged_stock_view, name='damaged_stock'),
    # Menu categories
    path('categories/', views.manage_categories, name='manage_categories'),
    # Stock log & cost history
    path('stock-log/', views.stock_log_view, name='stock_log'),
    path('cost-history/', views.cost_history_view, name='cost_history'),
    # Excel exports
    path('reports/excel/financial/', views.excel_report_financial, name='excel_report_financial'),
    path('reports/excel/ingredients/', views.excel_report_ingredients, name='excel_report_ingredients'),
    path('reports/excel/sales/', views.excel_report_sales, name='excel_report_sales'),
    path('reports/excel/stock-log/', views.excel_stock_log, name='excel_stock_log'),
    path('reports/excel/cost-history/', views.excel_cost_history, name='excel_cost_history'),
    path('reports/excel/activity-log/', views.excel_activity_log, name='excel_activity_log'),
    # Activity log
    path('activity-log/', views.activity_log_view, name='activity_log'),
    path('backup/export/', views.export_full_backup, name='export_full_backup'),
]
