import json
from pathlib import Path

from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken

user = get_user_model().objects.get(pk=117)
refresh = RefreshToken.for_user(user)
payload = {"access": str(refresh.access_token), "refresh": str(refresh)}
out = Path(r"C:\Users\abdur\AppData\Local\Temp\opencode\tokens.json")
out.write_text(json.dumps(payload), encoding="utf-8")
print("written", out)
