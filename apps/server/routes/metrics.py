"""Prometheus metrics exporter endpoint.

Exposes application metrics in Prometheus text exposition format at /metrics.
No external dependency - implements the text format directly to keep the
dependency surface minimal.

Metrics exposed:
- pinetunnel_http_requests_total{method,path,status} - Counter
- pinetunnel_http_request_duration_seconds{method,path} - Histogram
- pinetunnel_webhook_signals_total{command,result} - Counter
- pinetunnel_signal_queue_depth - Gauge
- pinetunnel_websocket_connections - Gauge
- pinetunnel_websocket_signals_delivered_total - Counter
- pinetunnel_redis_operations_total{operation,result} - Counter
- pinetunnel_db_queries_total - Counter
- pinetunnel_process_start_time_seconds - Gauge
"""

import time
import threading
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["metrics"])

_lock = threading.Lock()

_start_time = time.time()

_http_requests: dict[str, int] = defaultdict(int)
_http_durations: dict[str, list[float]] = defaultdict(list)
_webhook_signals: dict[str, int] = defaultdict(int)
_signal_queue_depth: int = 0
_ws_connections: int = 0
_ws_signals_delivered: int = 0
_redis_ops: dict[str, int] = defaultdict(int)
_db_queries: int = 0


def record_http_request(method: str, path: str, status: int, duration: float) -> None:
    key = f'{method}|{path}|{status}'
    with _lock:
        _http_requests[key] += 1
        _http_durations[key].append(duration)
        if len(_http_durations[key]) > 1000:
            _http_durations[key] = _http_durations[key][-500:]


def record_webhook_signal(command: str, result: str) -> None:
    key = f'{command}|{result}'
    with _lock:
        _webhook_signals[key] += 1


def set_signal_queue_depth(depth: int) -> None:
    global _signal_queue_depth
    with _lock:
        _signal_queue_depth = depth


def set_ws_connections(count: int) -> None:
    global _ws_connections
    with _lock:
        _ws_connections = count


def record_ws_delivery() -> None:
    global _ws_signals_delivered
    with _lock:
        _ws_signals_delivered += 1


def record_redis_op(operation: str, result: str) -> None:
    key = f'{operation}|{result}'
    with _lock:
        _redis_ops[key] += 1


def record_db_query() -> None:
    global _db_queries
    with _lock:
        _db_queries += 1


def _format_counter(name: str, labels: dict[str, str], value: int | float) -> str:
    label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return f'{name}{{{label_str}}} {value}'


def _format_gauge(name: str, labels: dict[str, str], value: int | float) -> str:
    label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return f'{name}{{{label_str}}} {value}'


def _format_histogram(name: str, labels: dict[str, str], durations: list[float]) -> str:
    if not durations:
        return ""
    buckets = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
    count = len(durations)
    total = sum(durations)
    lines = []
    label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
    for b in buckets:
        cumulative = sum(1 for d in durations if d <= b)
        lines.append(f'{name}_bucket{{{label_str},le="{b}"}} {cumulative}')
    lines.append(f'{name}_bucket{{{label_str},le="+Inf"}} {count}')
    lines.append(f'{name}_count{{{label_str}}} {count}')
    lines.append(f'{name}_sum{{{label_str}}} {total:.6f}')
    return "\n".join(lines)


def _build_metrics() -> str:
    lines = [
        "# HELP pinetunnel_process_start_time_seconds Unix timestamp of process start",
        "# TYPE pinetunnel_process_start_time_seconds gauge",
        f'pinetunnel_process_start_time_seconds {_start_time:.0f}',
        "",
        "# HELP pinetunnel_http_requests_total Total HTTP requests by method/path/status",
        "# TYPE pinetunnel_http_requests_total counter",
    ]

    with _lock:
        for key, count in sorted(_http_requests.items()):
            method, path, status = key.split("|")
            lines.append(_format_counter(
                "pinetunnel_http_requests_total",
                {"method": method, "path": path, "status": status},
                count,
            ))

        lines.append("")
        lines.append("# HELP pinetunnel_http_request_duration_seconds HTTP request duration")
        lines.append("# TYPE pinetunnel_http_request_duration_seconds histogram")
        for key, durations in sorted(_http_durations.items()):
            method, path, status = key.split("|")
            lines.append(_format_histogram(
                "pinetunnel_http_request_duration_seconds",
                {"method": method, "path": path, "status": status},
                durations,
            ))

        lines.append("")
        lines.append("# HELP pinetunnel_webhook_signals_total Total webhook signals by command/result")
        lines.append("# TYPE pinetunnel_webhook_signals_total counter")
        for key, count in sorted(_webhook_signals.items()):
            command, result = key.split("|")
            lines.append(_format_counter(
                "pinetunnel_webhook_signals_total",
                {"command": command, "result": result},
                count,
            ))

        lines.append("")
        lines.append("# HELP pinetunnel_signal_queue_depth Current signal queue depth")
        lines.append("# TYPE pinetunnel_signal_queue_depth gauge")
        lines.append(f'pinetunnel_signal_queue_depth {_signal_queue_depth}')

        lines.append("")
        lines.append("# HELP pinetunnel_websocket_connections Current WebSocket connections")
        lines.append("# TYPE pinetunnel_websocket_connections gauge")
        lines.append(f'pinetunnel_websocket_connections {_ws_connections}')

        lines.append("")
        lines.append("# HELP pinetunnel_websocket_signals_delivered_total Total signals delivered via WebSocket")
        lines.append("# TYPE pinetunnel_websocket_signals_delivered_total counter")
        lines.append(f'pinetunnel_websocket_signals_delivered_total {_ws_signals_delivered}')

        lines.append("")
        lines.append("# HELP pinetunnel_redis_operations_total Total Redis operations by type/result")
        lines.append("# TYPE pinetunnel_redis_operations_total counter")
        for key, count in sorted(_redis_ops.items()):
            operation, result = key.split("|")
            lines.append(_format_counter(
                "pinetunnel_redis_operations_total",
                {"operation": operation, "result": result},
                count,
            ))

        lines.append("")
        lines.append("# HELP pinetunnel_db_queries_total Total database queries executed")
        lines.append("# TYPE pinetunnel_db_queries_total counter")
        lines.append(f'pinetunnel_db_queries_total {_db_queries}')

    return "\n".join(lines) + "\n"


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics endpoint.

    Returns metrics in Prometheus text exposition format.
    Scrape with prometheus or curl: `curl http://host:8000/metrics`
    """
    return Response(content=_build_metrics(), media_type="text/plain; version=0.0.4; charset=utf-8")
