from django.contrib.auth.models import User
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        profile = getattr(user, "profile", None)
        token["role"] = profile.role if profile else ""
        token["tenant_id"] = profile.tenant_id if profile else None
        token["tenant_name"] = profile.tenant.name if profile and profile.tenant else ""
        token["name"] = user.get_full_name() or user.username
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        profile = getattr(self.user, "profile", None)
        data["user"] = {
            "id": self.user.id,
            "username": self.user.username,
            "name": self.user.get_full_name() or self.user.username,
            "role": profile.role if profile else "",
            "tenant": profile.tenant.name if profile and profile.tenant else "",
            "tenant_id": profile.tenant_id if profile else None,
            "must_change_password": profile.must_change_password if profile else False,
        }
        return data


class UserSerializer(serializers.ModelSerializer):
    role = serializers.CharField(source="profile.role", read_only=True)
    tenant = serializers.CharField(source="profile.tenant.name", read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "role", "tenant"]
