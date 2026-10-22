from rest_framework import serializers

from .models import Category, Item


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name"]


class DimensionsSerializer(serializers.Serializer):
    width = serializers.IntegerField(min_value=1, max_value=500)
    height = serializers.IntegerField(min_value=1, required=False)


class ItemSerializer(serializers.ModelSerializer):
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(), source="category", required=False, allow_null=True
    )
    dimensions = DimensionsSerializer(required=False)
    labels = serializers.ListField(
        child=serializers.CharField(max_length=20), required=False, help_text="free tags"
    )
    quantity = serializers.IntegerField(min_value=1, max_value=99, required=False)
    status = serializers.ChoiceField(choices=["draft", "live"], required=False)

    class Meta:
        model = Item
        fields = [
            "id",
            "title",
            "status",
            "category_id",
            "dimensions",
            "labels",
            "quantity",
            "created",
        ]
        read_only_fields = ["created"]

    def create(self, validated_data):
        if validated_data.get("dimensions") is not None:
            validated_data["dimensions"] = dict(validated_data["dimensions"])
        return Item.objects.create(**validated_data)

    def update(self, instance, validated_data):
        if "dimensions" in validated_data:
            validated_data["dimensions"] = dict(validated_data["dimensions"])
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()
        return instance


class ArchiveSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=20, required=False)
