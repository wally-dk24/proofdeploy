from django.contrib import admin
from django.urls import include, path, re_path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("items", views.ItemViewSet, basename="item")
router.register("categories", views.CategoryViewSet, basename="category")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", views.health),
    path("ping/", views.Ping.as_view()),
    path("legacy/<int:num>/<slug:slug>/", views.legacy),
    re_path(r"^plain/(?P<code>[0-9]+)/$", views.plain_code),
    path("api/", include(router.urls)),
]
