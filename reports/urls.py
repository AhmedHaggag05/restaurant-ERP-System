from django.urls import path
from . import views

app_name = 'reports'

urlpatterns = [
    path('',                          views.hub,                    name='hub'),
    path('finance/general/',          views.finance_general,        name='finance_general'),
    path('finance/custom/',           views.finance_custom,         name='finance_custom'),

    # PDF exports
    path('finance/general/pdf/',      views.finance_general_pdf,    name='finance_general_pdf'),
    path('finance/custom/pdf/',       views.finance_custom_pdf,     name='finance_custom_pdf'),
    path('purchasing/pdf/',           views.purchasing_pdf,         name='purchasing_pdf'),
    path('hr/attendance/pdf/',        views.hr_attendance_pdf,      name='hr_attendance_pdf'),
    path('hr/payroll/pdf/',           views.hr_payroll_pdf,         name='hr_payroll_pdf'),
    path('inventory/pdf/',            views.inventory_pdf,          name='inventory_pdf'),
    path('activity/pdf/',             views.activity_pdf,           name='activity_pdf'),

    # Excel exports
    path('finance/general/excel/',    views.finance_general_excel,  name='finance_general_excel'),
    path('finance/custom/excel/',     views.finance_custom_excel,   name='finance_custom_excel'),
    path('purchasing/excel/',         views.purchasing_excel,       name='purchasing_excel'),
    path('hr/attendance/excel/',      views.hr_attendance_excel,    name='hr_attendance_excel'),
    path('hr/payroll/excel/',         views.hr_payroll_excel,       name='hr_payroll_excel'),
    path('inventory/excel/',          views.inventory_excel,        name='inventory_excel'),
    path('activity/excel/',           views.activity_excel,         name='activity_excel'),

    # Ingredient detail report
    path('ingredient-detail/',         views.ingredient_detail,      name='ingredient_detail'),
    path('ingredient-detail/pdf/',     views.ingredient_detail_pdf,  name='ingredient_detail_pdf'),
    path('ingredient-detail/excel/',   views.ingredient_detail_excel,name='ingredient_detail_excel'),
]
