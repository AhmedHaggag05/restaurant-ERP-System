from django.urls import path
from . import views

app_name = 'accounting'

urlpatterns = [
    path('accounts/',       views.chart_of_accounts, name='chart_of_accounts'),
    path('journal/',        views.journal,            name='journal'),
    path('reports/pl/',     views.profit_loss,        name='profit_loss'),
    path('reports/bs/',     views.balance_sheet,      name='balance_sheet'),
]
