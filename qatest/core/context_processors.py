from django.conf import settings


def feature_flags(request):
    return {
        "FEATURE_HIDE_BATCH_EXEC": getattr(settings, "FEATURE_HIDE_BATCH_EXEC", False),
    }

