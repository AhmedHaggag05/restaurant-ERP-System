from django.urls import path
from . import views

app_name = 'hr'

urlpatterns = [
    path('employees/',                    views.employees,          name='employees'),
    path('attendance/',                   views.attendance,         name='attendance'),
    path('payroll/',                      views.salary_runs,        name='salary_runs'),
    path('payroll/create/',               views.create_salary_run,  name='create_salary_run'),
    path('payroll/<int:pk>/',             views.salary_run_detail,  name='salary_run_detail'),
    path('advances/',                     views.advances,           name='advances'),
]
