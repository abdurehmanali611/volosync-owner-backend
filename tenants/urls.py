from django.urls import path

from .owner_views import (
    BranchDetailView,
    BranchListView,
    BranchManagerPasswordView,
    OwnerReportsView,
    OwnerSessionView,
    SubscriptionEntryView,
)
from .views import TenantMeView

urlpatterns = [
    path("me/", TenantMeView.as_view()),
    path("owner/session/", OwnerSessionView.as_view()),
    path("owner/reports/", OwnerReportsView.as_view()),
    path("owner/subscription/", SubscriptionEntryView.as_view()),
    path("owner/branches/", BranchListView.as_view()),
    path("owner/branches/<int:pk>/", BranchDetailView.as_view()),
    path("owner/branches/<int:pk>/manager/password/", BranchManagerPasswordView.as_view()),
]
