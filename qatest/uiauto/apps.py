from django.apps import AppConfig


class UIAutoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "uiauto"

    def ready(self):
        import os
        from django.conf import settings

        if not settings.DEBUG:
            return
        if os.environ.get("RUN_MAIN") != "true":
            return
        from uiauto.services.execution_queue import start_worker
        start_worker()

