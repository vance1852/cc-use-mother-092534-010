"""荣誉公示后台的 HTTP/JSON 边界，组合基础层与荣誉层路由。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from festival_foundation import api as foundation_api
from festival_foundation.errors import DomainError, ValidationError

from .service import HonorService
from .storage import HonorDatabase


def route(service: HonorService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把 HTTP 请求分派到荣誉服务；未命中时回退到基础层路由。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    try:
        if method == "POST" and parsed.path == "/rules":
            receipt = service.freeze_rule(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/batches":
            receipt = service.create_batch(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/batches/close":
            receipt = service.close_batch(actor_id=actor_id, **body)
            return 200, receipt.__dict__
        if method == "POST" and parsed.path == "/batches/rebind-rule":
            receipt = service.rebind_rule(actor_id=actor_id, **body)
            return 200, receipt.__dict__
        if method == "POST" and parsed.path == "/conflicts":
            receipt = service.declare_conflict(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/consents/grant":
            receipt = service.grant_consent(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/consents/revoke":
            receipt = service.revoke_consent(actor_id=actor_id, **body)
            return 200, receipt.__dict__
        if method == "POST" and parsed.path == "/facts":
            receipt = service.submit_fact(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/assignments":
            receipt = service.assign_reviewer(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/assignments/cancel":
            receipt = service.cancel_assignment(actor_id=actor_id, **body)
            return 200, receipt.__dict__
        if method == "POST" and parsed.path == "/assignments/complete":
            receipt = service.complete_assignment(actor_id=actor_id, **body)
            return 200, receipt.__dict__
        if method == "POST" and parsed.path == "/fact-decisions":
            receipt = service.decide_fact(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/supplements":
            receipt = service.submit_supplement(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/final-decisions":
            receipt = service.confirm_selection(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/publications":
            receipt = service.publish(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/publications":
            batch_id = query.get("batch_id", [""])[0]
            if not batch_id:
                raise ValidationError("batch_id 不能为空")
            return 200, {"items": service.list_publications(batch_id)}
        if method == "GET" and parsed.path == "/internal/events":
            event_id = query.get("event_id", [""])[0]
            if not event_id:
                raise ValidationError("event_id 不能为空")
            return 200, service.get_event_trace(actor_id=actor_id, event_id=event_id)
        return foundation_api.route(service, method, path, body, headers)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: HonorService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动节日坚守人员贡献核验与荣誉公示后台")
    parser.add_argument("--database", default="festival_honor.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = HonorDatabase(args.database)
    Handler.service = HonorService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
