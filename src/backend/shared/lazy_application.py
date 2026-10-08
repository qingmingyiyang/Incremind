"""ASGI import entry without eagerly constructing a server user container."""
from threading import Lock


class LazyApplication:
    def __init__(self, factory):
        self._factory, self._application, self._lock = factory, None, Lock()

    @property
    def application(self):
        """Inspect the loaded instance without invoking its factory."""
        return self._application

    async def __call__(self, scope, receive, send):
        if self._application is None:
            with self._lock:
                if self._application is None:
                    self._application = self._factory()
        return await self._application(scope, receive, send)
