#!/usr/bin/env bash
# Run the bridge under Splunk OpenTelemetry Python auto-instrumentation.
# Requires: /opt/script/venv, /opt/script/env (from env.example), Splunk OTel collector optional but recommended on the host.
set -euo pipefail
cd /opt/script
# shellcheck source=/dev/null
if [[ -f /opt/script/env ]]; then
  set -a
  # shellcheck source=/dev/null
  source /opt/script/env
  set +a
fi

export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-oci-metrics-bridge-vm}"
export OTEL_PYTHON_DISTRO="${OTEL_PYTHON_DISTRO:-splunk_distro}"
export OTEL_TRACES_EXPORTER="${OTEL_TRACES_EXPORTER:-otlp}"
export OTEL_METRICS_EXPORTER="${OTEL_METRICS_EXPORTER:-otlp}"
export OTEL_EXPORTER_OTLP_PROTOCOL="${OTEL_EXPORTER_OTLP_PROTOCOL:-http/protobuf}"
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL="${OTEL_EXPORTER_OTLP_TRACES_PROTOCOL:-http/protobuf}"
export OTEL_EXPORTER_OTLP_METRICS_PROTOCOL="${OTEL_EXPORTER_OTLP_METRICS_PROTOCOL:-http/protobuf}"
export OTEL_TRACES_SAMPLER="${OTEL_TRACES_SAMPLER:-always_on}"
export OTEL_LOGS_EXPORTER="${OTEL_LOGS_EXPORTER:-none}"
export OTEL_RESOURCE_ATTRIBUTES="${OTEL_RESOURCE_ATTRIBUTES:-deployment.environment=gcp-lab,service.namespace=oci-bridge}"

exec /opt/script/venv/bin/opentelemetry-instrument /opt/script/venv/bin/python /opt/script/oci_metrics_bridge_vm.py "$@"
