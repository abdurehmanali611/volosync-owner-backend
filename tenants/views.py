from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import TenantSerializer


class TenantMeView(APIView):
    """The tenant the signed-in account belongs to, whatever their role.

    Everything an owner can reach lives in owner_views and is scoped to their
    own company. This project shares one live schema with VoloSync Admin, so it
    must never expose a bare tenant list or an id-addressable patch.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = getattr(request.user, "profile", None)
        if not profile or not profile.tenant:
            return Response({"data": None})
        return Response({"data": TenantSerializer(profile.tenant).data})



