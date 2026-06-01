"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import TemplateView # <-- ADD THIS IMPORT
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

handler400 = 'core.views.handler_400'
handler403 = 'core.views.handler_403'
handler404 = 'core.views.handler_404'
handler500 = 'core.views.handler_500'

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
    path("hr/", include("hr.urls")),
    path("purchasing/", include("purchasing.urls")),
    path("reports/", include("reports.urls")),
    path("accounting/", include("accounting.urls")),
# --- PWA ADDITIONS ---
    path('manifest.json', TemplateView.as_view(
        template_name='manifest.json',
        content_type='application/json'
    ), name='manifest'),
    path('sw.js', TemplateView.as_view(
        template_name='sw.js',
        content_type='application/javascript'
    ), name='sw'),
# --- PHASE 4: MOBILE API LAYER ---
    path('api/mobile/login/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/mobile/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/mobile/', include('core.api_urls')),
]

if settings.DEBUG:
    urlpatterns += static("/assets/", document_root=settings.BASE_DIR / "assets")
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)