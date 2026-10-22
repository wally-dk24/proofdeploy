from django.http import JsonResponse
from django.views import View
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Category, Item
from .serializers import ArchiveSerializer, CategorySerializer, ItemSerializer


class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all().order_by("id")
    serializer_class = CategorySerializer


class ItemViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.all().order_by("id")
    serializer_class = ItemSerializer

    @action(detail=True, methods=["post"], serializer_class=ArchiveSerializer)
    def archive(self, request, pk=None):
        item = self.get_object()
        item.status = "draft"
        item.save()
        return Response({"id": item.pk, "status": item.status})

    @action(detail=False, methods=["get"])
    def stats(self, request):
        return Response({"count": Item.objects.count()})


def health(request):
    return JsonResponse({"ok": True})


def legacy(request, num, slug):
    return JsonResponse({"num": num, "slug": slug})


def plain_code(request, code):
    return JsonResponse({"code": code})


class Ping(View):
    def get(self, request):
        return JsonResponse({"pong": True})

    def post(self, request):
        return JsonResponse({"pong": True}, status=201)
