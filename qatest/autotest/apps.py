from django.apps import AppConfig


class AutotestConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "autotest"

    def ready(self):
        import os
        import tempfile
        from django.conf import settings

        if not settings.DEBUG:
            return
        if os.environ.get("RUN_MAIN") != "true":
            return
        if os.name == "nt":
            try:
                import browser_use.browser.profile as bu_profile

                cls = getattr(bu_profile, "BrowserLaunchArgs", None) or getattr(bu_profile, "BrowserProfile", None)
                original = getattr(cls, "set_default_downloads_path", None) if cls is not None else None

                if original is not None:
                    def _patched(self):  # type: ignore[no-untyped-def]
                        if getattr(self, "downloads_path", None) is None:
                            import uuid
                            from pathlib import Path

                            base = Path(tempfile.gettempdir()) / "browser-use-downloads"
                            unique_id = str(uuid.uuid4())[:8]
                            downloads_path = base / f"browser-use-downloads-{unique_id}"
                            while downloads_path.exists():
                                unique_id = str(uuid.uuid4())[:8]
                                downloads_path = base / f"browser-use-downloads-{unique_id}"
                            self.downloads_path = downloads_path
                            self.downloads_path.mkdir(parents=True, exist_ok=True)
                        return self

                    target = getattr(original, "__func__", None) or original
                    target.__code__ = _patched.__code__
                    target.__defaults__ = _patched.__defaults__
                    target.__kwdefaults__ = _patched.__kwdefaults__
            except Exception:
                pass
        from autotest.services.execution_queue import start_worker
        start_worker()
