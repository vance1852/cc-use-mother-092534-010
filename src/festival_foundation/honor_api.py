"""荣誉核验与公示后台的 HTTP/JSON 边界。

公开接口（/public/*）不需要操作者身份，只输出获准字段；
内部接口（/internal/*）只对 admin、auditor 开放，还原完整依据；
写入接口通过 X-Actor-Id 标识操作者。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import api as foundation_api
from .errors import DomainError, ValidationError
from .honor_service import HonorService
from .honor_storage import HonorDatabase
from .service import DomainService


def route_honor(honor: HonorService, method: str, path: str, body: dict[str, Any],
                headers: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
    """识别荣誉领域路由；不匹配时返回 None 交给基础层路由。"""

    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    p = parsed.path

    if method == "POST" and p == "/honor/rules":
        return _result(honor.publish_rules(actor_id=actor_id, **body))
    if method == "POST" and p == "/conflicts":
        return _result(honor.declare_conflict(actor_id=actor_id, **body))
    if method == "POST" and p == "/honor/batches":
        return _result(honor.create_batch(actor_id=actor_id, **body))
    if method == "POST" and p == "/honor/batches/refresh-rules":
        return _result(honor.refresh_batch_rules(actor_id=actor_id, **body))
    if method == "POST" and p == "/honor/batches/finalize":
        return _result(honor.finalize_batch(actor_id=actor_id, **body))
    if method == "POST" and p == "/honor/batches/publish":
        return _result(honor.publish_batch(actor_id=actor_id, **body))
    if method == "POST" and p == "/consents":
        return _result(honor.grant_consent(actor_id=actor_id, **body))
    if method == "POST" and p == "/consents/revoke":
        return _result(honor.revoke_consent(actor_id=actor_id, **body))
    if method == "POST" and p == "/nominations":
        return _result(honor.submit_nomination(actor_id=actor_id, **body))
    if method == "POST" and p == "/verifications/assign":
        return _result(honor.assign_verifier(actor_id=actor_id, **body))
    if method == "POST" and p == "/facts/decide":
        return _result(honor.decide_fact(actor_id=actor_id, **body))
    if method == "POST" and p == "/supplements":
        return _result(honor.submit_supplement(actor_id=actor_id, **body))
    if method == "POST" and p == "/confirmations":
        return _result(honor.confirm_selection(actor_id=actor_id, **body))

    if method == "GET" and p == "/public/batches":
        return 200, {"items": honor.public_batches()}
    if method == "GET" and p == "/public/honors":
        batch_id = query.get("batch_id", [""])[0]
        if not batch_id:
            raise ValidationError("batch_id 不能为空")
        return 200, honor.public_honors(batch_id)
    if method == "GET" and p == "/internal/batches":
        batch_id = query.get("batch_id", [""])[0]
        if not batch_id:
            raise ValidationError("batch_id 不能为空")
        return 200, honor.batch_detail(actor_id=actor_id, batch_id=batch_id)
    if method == "GET" and p == "/internal/nominations":
        nomination_id = query.get("nomination_id", [""])[0]
        if not nomination_id:
            raise ValidationError("nomination_id 不能为空")
        return 200, honor.nomination_trace(actor_id=actor_id, nomination_id=nomination_id)
    return None


def _result(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200 if payload.get("replayed") else 201, payload


def route_combined(foundation: DomainService, honor: HonorService, method: str, path: str,
                   body: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """先匹配荣誉领域路由，再回退到基础层路由。"""

    try:
        matched = route_honor(honor, method, path, body, headers)
        if matched is not None:
            return matched
        return foundation_api.route(foundation, method, path, body, headers)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    foundation: DomainService
    honor: HonorService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route_combined(
            self.foundation, self.honor, self.command, self.path, body,
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
    parser = argparse.ArgumentParser(description="启动节日坚守人员贡献核验与荣誉公示后台")
    parser.add_argument("--database", default="honor.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = HonorDatabase(args.database)
    Handler.foundation = DomainService(database)
    Handler.honor = HonorService(database)
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
