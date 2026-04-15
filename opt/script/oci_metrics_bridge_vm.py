"""
OCI Monitoring → Splunk Observability (metrics) + Splunk Cloud (HEC), for a Linux VM.

Uses OCI API key authentication (~/.oci/config), not OCI resource principals.

Run with Splunk OTEL auto-instrumentation:
  /opt/script/run-with-otel.sh

Logs: stderr (for cron) plus a single JSON file overwritten each run (BRIDGE_JSON_LOG).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import oci
import requests
from oci.monitoring import MonitoringClient
from oci.monitoring.models import ListMetricsDetails, SummarizeMetricsDataDetails
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode


def _region_from_env() -> str:
    return os.environ.get("OCI_REGION", "") or "us-ashburn-1"


class TraceContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span()
        ctx = span.get_span_context() if span is not None else None
        if ctx is not None and ctx.is_valid:
            record.trace_id = format(ctx.trace_id, "032x")
            record.span_id = format(ctx.span_id, "016x")
        else:
            record.trace_id = ""
            record.span_id = ""
        return True


@dataclass
class RunContext:
    started: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    entries: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "unknown"
    processed_metric_definitions: Optional[int] = None
    error: Optional[str] = None

    def add_log(self, record: logging.LogRecord) -> None:
        self.entries.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "trace_id": getattr(record, "trace_id", ""),
                "span_id": getattr(record, "span_id", ""),
            }
        )

    def write(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, mode=0o755, exist_ok=True)
        payload = {
            "started_at": self.started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "hostname": socket.gethostname(),
            "status": self.status,
            "processed_metric_definitions": self.processed_metric_definitions,
            "error": self.error,
            "log_entries": self.entries,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)


class JsonRunLogHandler(logging.Handler):
    def __init__(self, ctx: RunContext) -> None:
        super().__init__(level=logging.DEBUG)
        self.ctx = ctx

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ctx.add_log(record)
        except Exception:
            self.handleError(record)


def setup_logging(run_ctx: RunContext) -> logging.Logger:
    log = logging.getLogger("oci_metrics_bridge")
    log.handlers.clear()
    log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    log.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s trace_id=%(trace_id)s span_id=%(span_id)s %(message)s"
    )
    flt = TraceContextFilter()

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG)
    sh.addFilter(flt)
    sh.setFormatter(fmt)

    jh = JsonRunLogHandler(run_ctx)
    jh.addFilter(flt)
    jh.setFormatter(fmt)

    log.addHandler(sh)
    log.addHandler(jh)
    return log


def _hec_verify() -> bool:
    return os.environ.get("SPLUNK_HEC_INSECURE_SKIP_VERIFY", "").lower() not in ("1", "true", "yes")


def send_hec_event(
    log: logging.Logger,
    message: str,
    level: str = "INFO",
    extra_fields: Optional[Dict[str, Any]] = None,
) -> None:
    url = os.environ.get("SPLUNK_HEC_URL", "").strip()
    token = os.environ.get("SPLUNK_HEC_TOKEN", "").strip()
    if not url or not token:
        log.warning("HEC URL or token not set; skipping HEC log")
        return

    span = trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    trace_id = format(ctx.trace_id, "032x") if ctx and ctx.is_valid else ""
    span_id = format(ctx.span_id, "016x") if ctx and ctx.is_valid else ""

    event_obj: Dict[str, Any] = {
        "message": message,
        "level": level,
        "trace_id": trace_id,
        "span_id": span_id,
        "component": "oci-metrics-bridge-vm",
    }
    if extra_fields:
        event_obj.update(extra_fields)

    hec_index = os.environ.get("SPLUNK_HEC_INDEX", "main").strip()
    body: Dict[str, Any] = {
        "time": int(time.time()),
        "host": os.environ.get("SPLUNK_HEC_HOST", socket.gethostname()),
        "source": os.environ.get("SPLUNK_HEC_SOURCE", "oci:metrics-bridge-vm"),
        "sourcetype": "oci:metrics-bridge:json",
        "event": event_obj,
        "fields": {
            "level": level,
            "trace_id": trace_id,
            "span_id": span_id,
            "component": "oci-metrics-bridge-vm",
        },
    }
    if hec_index:
        body["index"] = hec_index

    hec_tracer = trace.get_tracer(__name__)
    with hec_tracer.start_as_current_span(
        "splunk_cloud.hec_submit",
        attributes={"splunk.hec.level": level},
    ) as hec_span:
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Splunk {token}"},
                json=body,
                timeout=15,
                verify=_hec_verify(),
            )
            hec_span.set_attribute("http.status_code", r.status_code)
            if r.status_code < 300:
                log.info("HEC event accepted http_status=%s", r.status_code)
            else:
                log.error("HEC post failed status=%s body=%s", r.status_code, r.text[:500])
                hec_span.set_status(Status(StatusCode.ERROR, f"HTTP {r.status_code}"))
        except Exception as e:
            hec_span.record_exception(e)
            hec_span.set_status(Status(StatusCode.ERROR, str(e)))
            log.exception("HEC post raised")


def _build_query(metric_name: str, dimensions: Dict[str, str], window: str) -> str:
    if dimensions:
        dim_pairs = ",".join(f'{k}="{v}"' for k, v in dimensions.items())
        return f"{metric_name}[{window}]{{{dim_pairs}}}.mean()"
    return f"{metric_name}[{window}].mean()"


def _send_signalfx_gauges(
    log: logging.Logger,
    realm: str,
    token: str,
    gauges: List[Dict[str, Any]],
) -> None:
    if not gauges:
        return
    url = f"https://ingest.{realm}.signalfx.com/v2/datapoint"
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(
        "splunk_o11y.ingest_datapoints",
        attributes={
            "signalfx.realm": realm,
            "signalfx.url": url,
            "signalfx.gauge_count": len(gauges),
        },
    ) as span:
        r = requests.post(
            url,
            headers={"X-SF-Token": token, "Content-Type": "application/json"},
            data=json.dumps({"gauge": gauges}),
            timeout=30,
        )
        span.set_attribute("http.status_code", r.status_code)
        if r.status_code >= 300:
            log.error("SignalFx ingest failed status=%s body=%s", r.status_code, r.text[:800])
            span.set_status(Status(StatusCode.ERROR, f"HTTP {r.status_code}"))
            raise RuntimeError(f"SignalFx ingest HTTP {r.status_code}")


def _dims_preview(dims: Dict[str, str], max_len: int = 400) -> str:
    s = json.dumps(dims, sort_keys=True)
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def collect_and_forward(log: logging.Logger) -> int:
    compartment = os.environ.get("METRICS_COMPARTMENT_OCID", "").strip()
    if not compartment:
        raise ValueError("METRICS_COMPARTMENT_OCID is not set")

    realm = os.environ.get("SPLUNK_REALM", "us1").strip()
    token = os.environ.get("SPLUNK_ACCESS_TOKEN", "").strip()
    if not token:
        raise ValueError("SPLUNK_ACCESS_TOKEN is not set")

    max_metrics = int(os.environ.get("MAX_METRICS_PER_INVOKE", "75"))
    window_min = int(os.environ.get("OCI_METRICS_WINDOW_MINUTES", "5"))
    window = f"{window_min}m"

    cfg_path = os.path.expanduser(os.environ.get("OCI_CONFIG_FILE", oci.config.DEFAULT_LOCATION))
    profile = os.environ.get("OCI_CONFIG_PROFILE", oci.config.DEFAULT_PROFILE)
    if not os.path.isfile(cfg_path):
        raise FileNotFoundError(f"OCI config missing: {cfg_path} (add API key profile for metrics read)")
    oci_config = oci.config.from_file(cfg_path, profile_name=profile)
    client = MonitoringClient(oci_config, timeout=(10, 60))
    region = oci_config.get("region") or _region_from_env()
    tracer = trace.get_tracer(__name__)

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_min)
    start_s = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_s = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    in_subtree = os.environ.get("LIST_METRICS_IN_SUBTREE", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    log.info(
        "Starting OCI metrics collection compartment=%s region=%s window=%s max_metric_definitions=%s list_metrics_subtree=%s",
        compartment,
        region,
        window,
        max_metrics,
        in_subtree,
    )
    send_hec_event(
        log,
        "metrics collection started",
        extra_fields={
            "compartment_id": compartment,
            "region": region,
            "list_metrics_in_subtree": str(in_subtree),
        },
    )

    details = ListMetricsDetails()
    metrics_seen = 0
    gauges: List[Dict[str, Any]] = []
    opc_next_page: Optional[str] = None
    list_page_index = 0
    datapoint_count = 0
    definitions_with_points = 0
    definitions_no_points = 0
    summarize_failures = 0

    while metrics_seen < max_metrics:
        kwargs: Dict[str, Any] = {
            "compartment_id": compartment,
            "list_metrics_details": details,
        }
        if in_subtree:
            kwargs["compartment_id_in_subtree"] = True
        if opc_next_page:
            kwargs["page"] = opc_next_page

        try:
            with tracer.start_as_current_span(
                "oci.monitoring.list_metrics",
                attributes={
                    "oci.compartment_id": compartment,
                    "oci.region": region,
                    "oci.list_metrics.page_index": list_page_index,
                    "oci.list_metrics.subtree": in_subtree,
                },
            ) as lm_span:
                try:
                    lm = client.list_metrics(**kwargs)
                except oci.exceptions.ServiceError as e:
                    lm_span.set_attribute("oci.error.code", e.code)
                    lm_span.record_exception(e)
                    lm_span.set_status(Status(StatusCode.ERROR, e.message))
                    raise
                lm_span.set_attribute("oci.list_metrics.definitions_returned", len(lm.data or []))
        except oci.exceptions.ServiceError as e:
            log.error("list_metrics ServiceError code=%s message=%s", e.code, e.message)
            send_hec_event(
                log,
                f"list_metrics failed: {e.message}",
                level="ERROR",
                extra_fields={"oci_code": e.code},
            )
            raise

        items = lm.data or []
        if not items:
            if list_page_index == 0:
                log.warning(
                    "EMPTY_METRIC_LIST: list_metrics returned no metric definitions for compartment=%s region=%s subtree=%s.",
                    compartment,
                    region,
                    in_subtree,
                )
            else:
                log.info("list_metrics page=%s empty (end of pagination)", list_page_index)
            break

        log.info("list_metrics page=%s definitions_on_page=%s", list_page_index, len(items))
        list_page_index += 1

        for item in items:
            if metrics_seen >= max_metrics:
                break
            metrics_seen += 1
            name = item.name
            ns = item.namespace
            dims = dict(item.dimensions or {})

            log.info(
                "collecting definition %s/%s dimensions=%s",
                ns,
                name,
                _dims_preview(dims),
            )

            query = _build_query(name, dims, window)
            points_before = datapoint_count
            try:
                with tracer.start_as_current_span(
                    "oci.monitoring.summarize_metrics_data",
                    attributes={
                        "oci.metric.namespace": str(ns),
                        "oci.metric.name": str(name),
                        "oci.metrics.query": query[:1200],
                    },
                ) as sm_span:
                    try:
                        sm = client.summarize_metrics_data(
                            compartment_id=compartment,
                            summarize_metrics_data_details=SummarizeMetricsDataDetails(
                                namespace=ns,
                                query=query,
                                start_time=start_s,
                                end_time=end_s,
                            ),
                        )
                    except oci.exceptions.ServiceError as e:
                        sm_span.set_attribute("oci.error.code", e.code)
                        sm_span.record_exception(e)
                        sm_span.set_status(Status(StatusCode.ERROR, e.message))
                        raise
                    series_n = len(sm.data or [])
                    sm_span.set_attribute("oci.summarize.series_count", series_n)
            except oci.exceptions.ServiceError as e:
                summarize_failures += 1
                log.warning(
                    "summarize failed for metric=%s ns=%s code=%s msg=%s",
                    name,
                    ns,
                    e.code,
                    e.message,
                )
                continue
            except Exception:
                summarize_failures += 1
                log.exception("summarize unexpected error metric=%s ns=%s", name, ns)
                continue

            for series in sm.data or []:
                for dp in series.aggregated_datapoints or []:
                    if getattr(dp, "timestamp", None):
                        ts = dp.timestamp
                        if isinstance(ts, datetime):
                            ts_ms = int(ts.timestamp() * 1000)
                        else:
                            ts_ms = int(time.time() * 1000)
                    else:
                        ts_ms = int(time.time() * 1000)
                    try:
                        val = float(dp.value)
                    except (TypeError, ValueError):
                        continue
                    metric_key = f"oci.{ns.replace('/', '.')}.{name}"
                    sf_dims = {
                        **{k: str(v) for k, v in dims.items()},
                        "oci_namespace": str(ns),
                        "metric_name": str(name),
                    }
                    gauges.append(
                        {
                            "metric": metric_key,
                            "dimensions": sf_dims,
                            "value": val,
                            "timestamp": ts_ms,
                        }
                    )
                    datapoint_count += 1
                    log.debug(
                        "datapoint ns=%s metric=%s value=%s ts_ms=%s signalfx_key=%s",
                        ns,
                        name,
                        val,
                        ts_ms,
                        metric_key,
                    )

            if datapoint_count > points_before:
                definitions_with_points += 1
            else:
                definitions_no_points += 1
                log.info(
                    "no datapoints in window for ns=%s name=%s (summarize ok but empty series; try wider window or later retry)",
                    ns,
                    name,
                )

            if len(gauges) >= 100:
                _send_signalfx_gauges(log, realm, token, gauges)
                gauges.clear()

        opc_next_page = lm.next_page
        if not opc_next_page:
            break

    if gauges:
        _send_signalfx_gauges(log, realm, token, gauges)
    elif datapoint_count == 0:
        with tracer.start_as_current_span(
            "splunk_o11y.ingest_datapoints",
            attributes={"signalfx.skipped": True, "signalfx.skip_reason": "no_datapoints"},
        ):
            pass

    log.info(
        "Finished metrics collection metric_definitions_scanned=%s datapoints_to_signalfx=%s "
        "definitions_with_points=%s definitions_no_points_in_window=%s summarize_failures=%s list_pages=%s",
        metrics_seen,
        datapoint_count,
        definitions_with_points,
        definitions_no_points,
        summarize_failures,
        list_page_index,
    )
    send_hec_event(
        log,
        "metrics collection finished",
        extra_fields={
            "processed_metric_definitions": metrics_seen,
            "datapoints_forwarded": datapoint_count,
            "definitions_with_points": definitions_with_points,
            "definitions_no_points_in_window": definitions_no_points,
        },
    )
    return metrics_seen


def _log_otel_trace_hints(log: logging.Logger) -> None:
    for key in (
        "OTEL_SERVICE_NAME",
        "OTEL_TRACES_EXPORTER",
        "OTEL_TRACES_SAMPLER",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "SPLUNK_REALM",
    ):
        v = os.environ.get(key, "").strip()
        if v:
            log.info("otel_config %s=%s", key, v)
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        log.info(
            "otel_config OTEL_EXPORTER_OTLP_ENDPOINT not set (Splunk distro usually sets OTLP from SPLUNK_REALM + access token)"
        )


def main() -> int:
    run_ctx = RunContext()
    log_path = os.environ.get("BRIDGE_JSON_LOG", "/opt/script/logs/last-run.json")
    log = setup_logging(run_ctx)
    _log_otel_trace_hints(log)
    log.info(
        "bridge_vm_start realm=%s hec_configured=%s access_token_configured=%s compartment_configured=%s",
        os.environ.get("SPLUNK_REALM", ""),
        bool(os.environ.get("SPLUNK_HEC_URL", "").strip() and os.environ.get("SPLUNK_HEC_TOKEN", "").strip()),
        bool(os.environ.get("SPLUNK_ACCESS_TOKEN", "").strip()),
        bool(os.environ.get("METRICS_COMPARTMENT_OCID", "").strip()),
    )
    try:
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("oci_metrics_bridge_invoke"):
            processed = collect_and_forward(log)
        run_ctx.status = "ok"
        run_ctx.processed_metric_definitions = processed
        log.info("run complete processed_metric_definitions=%s", processed)
        return 0
    except Exception as e:
        run_ctx.status = "error"
        run_ctx.error = str(e)
        log.exception("run failure: %s", e)
        try:
            send_hec_event(
                log,
                f"run failure: {e}",
                level="ERROR",
                extra_fields={"error_type": type(e).__name__},
            )
        except Exception:
            log.exception("secondary failure sending HEC error event")
        return 1
    finally:
        try:
            run_ctx.write(log_path)
        except Exception:
            logging.getLogger("oci_metrics_bridge").exception("failed to write %s", log_path)


if __name__ == "__main__":
    sys.exit(main())
