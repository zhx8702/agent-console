import threading
import time

from client.auth_client import AuthClient, AuthClientError


class AuthorizationError(RuntimeError):
    pass


class RuntimeAuthGuard:
    def __init__(self, settings):
        self.settings = settings
        self.auth = AuthClient(settings)
        self.heartbeat_interval = settings.get("heartbeat_interval_sec", 60)
        self.auth_grace_sec = settings.get("auth_grace_sec", 180)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_ok_ts = 0.0
        self._last_error = ""
        self._session_token = ""
        self._session_claims = {}

    def start(self):
        self._refresh_session(initial=True)
        self._thread = threading.Thread(target=self._run_heartbeat, name="auth-heartbeat", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def require(self, capability):
        with self._lock:
            claims = dict(self._session_claims or {})
            expires_ts = float(claims.get("expires_ts", 0) or 0)
            allowed_caps = set(claims.get("capabilities") or [])
            deadline = min(expires_ts or 0, self._last_ok_ts + self.auth_grace_sec) if expires_ts else 0
            if self._session_token and deadline and time.time() <= deadline and capability in allowed_caps:
                return
            if self._session_token and capability not in allowed_caps:
                detail = f"capability not granted: {capability}"
            elif self._session_token and (time.time() - self._last_ok_ts) > self.auth_grace_sec:
                detail = self._last_error or "authorization heartbeat expired"
            elif self._session_token and expires_ts and time.time() > expires_ts:
                detail = self._last_error or "signed session expired"
            else:
                detail = self._last_error or "no valid online session"
        raise AuthorizationError(f"{capability} blocked: {detail}")

    def status_snapshot(self):
        with self._lock:
            return {
                "last_ok_ts": self._last_ok_ts,
                "last_error": self._last_error,
                "has_session": bool(self._session_token),
                "capabilities": list((self._session_claims or {}).get("capabilities") or []),
            }

    def _run_heartbeat(self):
        while not self._stop.wait(self.heartbeat_interval):
            self._refresh_session(initial=False)

    def _refresh_session(self, initial):
        try:
            if initial:
                body = self.auth.login()
            else:
                body = self.auth.heartbeat()
            with self._lock:
                self._session_token = body["session_token"]
                self._session_claims = dict(body or {})
                self._last_ok_ts = time.time()
                self._last_error = ""
            print("[auth] session refreshed")
        except AuthClientError as e:
            with self._lock:
                self._last_error = str(e)
            print("[auth] refresh failed")
            if initial:
                raise AuthorizationError(str(e)) from e
