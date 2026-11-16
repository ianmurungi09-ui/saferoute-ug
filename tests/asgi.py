"""Tiny ASGI test clients (the usual httpx-based TestClient isn't available offline)."""
import asyncio
import json


class Client:
    def __init__(self, app):
        self.app = app

    async def call(self, method, path, body=None, headers=None, query="", raw=None):
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        hdrs = [(b"content-type", b"application/json")] + [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
                 "headers": hdrs, "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": ""}
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        out = {"status": None, "body": b""}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        await self.app(scope, receive, send)
        data = json.loads(out["body"]) if out["body"] else None
        return out["status"], data


class WSClient:
    def __init__(self, app, path, query=""):
        self.app = app
        self.inbox, self.outbox = asyncio.Queue(), asyncio.Queue()
        self.scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": path,
                      "raw_path": path.encode(), "query_string": query.encode(), "headers": [],
                      "client": ("127.0.0.1", 1), "server": ("test", 80), "subprotocols": [], "root_path": ""}
        self.first = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.app(self.scope, self.inbox.get, self.outbox.put))
        await self.inbox.put({"type": "websocket.connect"})
        self.first = await asyncio.wait_for(self.outbox.get(), 2)
        return self

    @property
    def accepted(self):
        return self.first["type"] == "websocket.accept"

    async def receive_json(self, timeout=2):
        msg = await asyncio.wait_for(self.outbox.get(), timeout)
        return json.loads(msg["text"])

    async def __aexit__(self, *exc):
        if self.accepted:
            await self.inbox.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 2)
