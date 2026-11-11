import json
from pathlib import Path

from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken

user = get_user_model().objects.get(pk=117)
refresh = RefreshToken.for_user(user)
Path(r"C:\Users\abdur\AppData\Local\Temp\opencode\deploy_token.json").write_text(
    json.dumps({"access": str(refresh.access_token)}), encoding="utf-8"
)
print("ok")
