"""Tests for the Prometheus metrics exporter (apps/server/routes/metrics.py).

Tests cover:
- Metric recording functions
- Text exposition format output
- Counter and gauge formatting
"""

import pytest

from apps.server.routes.metrics import (
    record_http_request,
    record_webhook_signal,
    set_signal_queue_depth,
    set_ws_connections,
    record_ws_delivery,
    record_redis_op,
    record_db_query,
    _build_metrics,
)


class TestMetricsRecording:
    """Test metric recording functions."""

    def test_record_http_request(self):
        record_http_request("GET", "/health", 200, 0.005)
        metrics = _build_metrics()
        assert "pinetunnel_http_requests_total" in metrics
        assert 'method="GET"' in metrics
        assert 'status="200"' in metrics

    def test_record_webhook_signal(self):
        record_webhook_signal("buy", "ok")
        metrics = _build_metrics()
        assert "pinetunnel_webhook_signals_total" in metrics
        assert 'command="buy"' in metrics
        assert 'result="ok"' in metrics

    def test_set_signal_queue_depth(self):
        set_signal_queue_depth(5)
        metrics = _build_metrics()
        assert "pinetunnel_signal_queue_depth 5" in metrics

    def test_set_ws_connections(self):
        set_ws_connections(3)
        metrics = _build_metrics()
        assert "pinetunnel_websocket_connections 3" in metrics

    def test_record_ws_delivery(self):
        record_ws_delivery()
        metrics = _build_metrics()
        assert "pinetunnel_websocket_signals_delivered_total" in metrics

    def test_record_redis_op(self):
        record_redis_op("ping", "ok")
        metrics = _build_metrics()
        assert "pinetunnel_redis_operations_total" in metrics

    def test_record_db_query(self):
        record_db_query()
        metrics = _build_metrics()
        assert "pinetunnel_db_queries_total" in metrics


class TestMetricsFormat:
    """Test Prometheus text exposition format."""

    def test_metrics_contains_help(self):
        metrics = _build_metrics()
        assert "# HELP" in metrics
        assert "# TYPE" in metrics

    def test_metrics_contains_process_start(self):
        metrics = _build_metrics()
        assert "pinetunnel_process_start_time_seconds" in metrics

    def test_metrics_has_histogram(self):
        record_http_request("POST", "/", 200, 0.15)
        metrics = _build_metrics()
        assert "pinetunnel_http_request_duration_seconds_bucket" in metrics
        assert "pinetunnel_http_request_duration_seconds_count" in metrics
        assert "pinetunnel_http_request_duration_seconds_sum" in metrics
