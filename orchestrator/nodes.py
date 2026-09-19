"""Identical HTTP transport for Pi and conveyor. No automatic command retries."""
import json
import urllib.error
import urllib.parse
import urllib.request


class NodeError(RuntimeError):
    pass


class NodeClient:
    def __init__(self, base_url, *, token=None, timeout=300, opener=None):
        url = urllib.parse.urlparse(base_url)
        if url.scheme not in ("http", "https") or not url.hostname or "<" in base_url or url.username or url.password:
            raise ValueError("Configure a concrete http(s) node base_url, without credentials")
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout
        # LAN requests must not be silently sent through a system proxy or redirected.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method, path, payload=None, *, timeout=None):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        body = None
        if payload is not None:
            body = json.dumps(payload, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise NodeError("Oversized node response")
                value = json.loads(raw)
                if not isinstance(value, dict) or value.get("ok") is False or "error" in value:
                    raise NodeError(f"Node rejected {path}: {value}")
                return value
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise NodeError(f"{method} {self.base_url}{path} failed; NOT retried: {exc}") from exc

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, payload):
        return self.call("POST", path, payload)
