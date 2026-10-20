# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""HTTP API.

    GET  /healthz
    GET  /v1/monitors
    GET  /v1/monitors/{name}
    GET  /v1/monitors/{name}/alerts?limit=100
    POST /v1/monitors/{name}/observations

The body of a POST is ``{"values": [0.21, 0.19, ...]}`` or, with event times,
``{"observations": [{"value": 0.21, "time": 1760000000.0}, ...]}``.  Values
are taken in the order given, and a batch is accepted whole.  The answer
gives the number accepted, the number the detector skipped as invalid, and
the alerts the batch raised.

There is no authentication: bind to localhost, or put the service behind a
proxy that authenticates.
"""
from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .monitor import MonitorError
from .service import Service, UnknownMonitor
from .version import __version__

logger = logging.getLogger("sequentialist")

MAX_BODY = 16 * 2**20


class BadRequest(ValueError):
    pass


def _constant(name: str) -> Any:
    raise BadRequest(f"{name} is not a number a detector can use")


def parse_observations(body: bytes) -> list[dict[str, Any]]:
    try:
        data = json.loads(body, parse_constant=_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BadRequest(f"not valid JSON: {error}") from None
    if not isinstance(data, dict) or len(data) != 1 or not ({"values", "observations"} & set(data)):
        raise BadRequest('expected {"values": [...]} or {"observations": [...]}')
    if "values" in data:
        if not isinstance(data["values"], list):
            raise BadRequest('"values" must be a list')
        return [{"value": v} for v in data["values"]]
    items = data["observations"]
    if not isinstance(items, list):
        raise BadRequest('"observations" must be a list')
    for i, item in enumerate(items):
        if not isinstance(item, dict) or "value" not in item or not set(item) <= {"value", "time"}:
            raise BadRequest(f'observation {i}: expected {{"value": ..., "time": ...}} (time optional)')
    return items


class Handler(BaseHTTPRequestHandler):
    service: Service  # set on the subclass made by make_server
    server_version = f"sequentialist/{__version__}"

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s %s", self.address_string(), format % args)

    def _send(self, status: int, body: Any) -> None:
        data = (json.dumps(body) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _route(self) -> tuple[list[str], dict[str, list[str]]]:
        url = urlsplit(self.path)
        return [p for p in url.path.split("/") if p], parse_qs(url.query)

    def do_GET(self) -> None:
        parts, query = self._route()
        try:
            if parts == ["healthz"]:
                self._send(200, {"status": "ok", "version": __version__})
            elif parts == ["v1", "monitors"]:
                self._send(200, {"monitors": [m.status() for m in self.service.monitors.values()]})
            elif len(parts) == 3 and parts[:2] == ["v1", "monitors"]:
                self._send(200, self.service.monitor(parts[2]).status())
            elif len(parts) == 4 and parts[:2] == ["v1", "monitors"] and parts[3] == "alerts":
                try:
                    limit = int(query.get("limit", ["100"])[0])
                except ValueError:
                    return self._error(400, "limit must be an integer")
                self._send(200, {"alerts": self.service.monitor(parts[2]).alerts(limit)})
            else:
                self._error(404, "not found")
        except UnknownMonitor as error:
            self._error(404, f"no monitor {error.args[0]!r}")

    def do_POST(self) -> None:
        parts, _ = self._route()
        if not (len(parts) == 4 and parts[:2] == ["v1", "monitors"] and parts[3] == "observations"):
            return self._error(404, "not found")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
        if length > MAX_BODY:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"at most {MAX_BODY} bytes per request")
        try:
            items = parse_observations(self.rfile.read(length))
            alerts, counts = self.service.ingest(parts[2], items)
        except BadRequest as error:
            return self._error(400, str(error))
        except UnknownMonitor as error:
            return self._error(404, f"no monitor {error.args[0]!r}")
        except MonitorError as error:
            return self._error(409, str(error))
        self._send(200, {**counts, "alerts": [a.to_json() for a in alerts]})


def make_server(service: Service, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
