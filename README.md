# OCI-VM — OCI metrics bridge on a Linux VM (GCP lab)

This repository holds a **Python port** of the serverless [ps-dba-client/OCI](https://github.com/ps-dba-client/OCI) metrics bridge: it calls **OCI Monitoring** (list + summarize), forwards gauges to **Splunk Observability** (SignalFx ingest), and sends structured events to **Splunk Cloud** via **HEC**.

The serverless version uses **OCI resource principals**. On a **GCP** (or any non-OCI) VM you must use an **OCI IAM user API key** in `~/.oci/config` with policies that allow **read/inspect metrics** on the target compartment.

## Layout (mirrors deployment path)

| Path in repo | On the VM after install |
|--------------|-------------------------|
| `opt/script/oci_metrics_bridge_vm.py` | `/opt/script/oci_metrics_bridge_vm.py` |
| `opt/script/requirements.txt` | `/opt/script/requirements.txt` |
| `opt/script/run-with-otel.sh` | `/opt/script/run-with-otel.sh` |
| `opt/script/env.example` | Copy to `/opt/script/env` (secrets, not committed) |

Install copies files to **`/opt/script/`** and creates a Python **venv** there.

## 1. Deploy the GCP Linux VM (Splunk OTel lab)

Use the Terraform stack under **[HL/gcp/linux-splunk-otel-lab](https://github.com/ps-dba-client)** (same tree as your consultant workspace) or equivalent:

```bash
cd gcp/linux-splunk-otel-lab/terraform
cp terraform.tfvars.example terraform.tfvars
# Set project_id, region, zone, allowed_ingress_cidr (your /32), etc.
terraform init
terraform apply
```

After apply, **SSH** to the instance and install the **Splunk OpenTelemetry Collector** with **instrumentation** enabled so host and language auto-instrumentation work as designed, for example:

```bash
# On the VM (example — use your lab’s install script / token flow)
export SPLUNK_ACCESS_TOKEN="***"
export SPLUNK_REALM="us1"
sudo -E ./install-splunk-otel-collector.sh install --realm us1 --with-instrumentation
```

Reference scripts: `gcp/linux-splunk-otel-lab/scripts/install-splunk-otel-collector.sh` in the workspace.

## 2. Install this bridge to `/opt/script`

On the VM (as **root**):

```bash
sudo apt-get update && sudo apt-get install -y git python3 python3-venv rsync
sudo git clone https://github.com/ps-dba-client/OCI-VM.git /usr/local/src/OCI-VM
cd /usr/local/src/OCI-VM
sudo ./install-to-opt.sh
```

## 3. OCI API key on the VM

Create an **OCI IAM user** (or reuse a technical user) and add an **API key**. On the VM:

```bash
sudo mkdir -p /root/.oci
sudo chmod 0700 /root/.oci
# Place private key PEM and create config — example:
sudo nano /root/.oci/config
```

Minimal `~/.oci/config`:

```ini
[DEFAULT]
user=ocid1.user.oc1..xxx
fingerprint=aa:bb:...
tenancy=ocid1.tenancy.oc1..xxx
region=us-ashburn-1
key_file=/root/.oci/oci_api_key.pem
```

**IAM policy** (in OCI) must allow this user to **read** and **inspect** metrics in `METRICS_COMPARTMENT_OCID` (and to use `compartment_id_in_subtree` when listing from root — same rules as the serverless sample).

## 4. Configure `/opt/script/env`

```bash
sudo cp /opt/script/env.example /opt/script/env
sudo chmod 600 /opt/script/env
sudo nano /opt/script/env
```

Set at least: `SPLUNK_REALM`, `SPLUNK_ACCESS_TOKEN`, `SPLUNK_HEC_URL`, `SPLUNK_HEC_TOKEN`, `METRICS_COMPARTMENT_OCID`, and `LIST_METRICS_IN_SUBTREE=true` only when scanning from the **tenancy root**.

## 5. Test (Splunk OTel auto-instrumentation)

```bash
sudo /opt/script/run-with-otel.sh
echo $?
sudo cat /opt/script/logs/last-run.json
```

Expect:

- **Splunk Observability**: traces for `oci-metrics-bridge-vm` with spans such as `oci.monitoring.*`, `splunk_o11y.ingest_datapoints`, `splunk_cloud.hec_submit`.
- **Splunk Cloud**: HEC events (`source` / `sourcetype` from env).
- **Local JSON**: `/opt/script/logs/last-run.json` overwritten each run with `status`, counts, and `log_entries`.

Run **without** `opentelemetry-instrument` only for debugging (no OTLP traces):

```bash
sudo /opt/script/venv/bin/python /opt/script/oci_metrics_bridge_vm.py
```

## 6. Cron (after validation)

```bash
sudo cp /opt/script/cron.example /etc/cron.d/oci-metrics-bridge
sudo chmod 0644 /etc/cron.d/oci-metrics-bridge
```

Adjust schedule as needed. Cron stdout/stderr append to `/opt/script/logs/cron.log`; the structured run log still overwrites **`last-run.json`** each execution.

## OCI Terraform destroy (serverless stack)

The companion **OCI** sample can be torn down when moving to a VM-only client workflow:

```bash
cd oci/terraform
terraform destroy
```

## Security

- Do **not** commit `/opt/script/env`, API private keys, or Splunk tokens.
- Restrict SSH (`allowed_ingress_cidr`) and use least-privilege OCI policies.

## Disclaimer

Sample code for demos and client adaptation—not production-hardened for secrets storage (use Vault / secret manager for long-term deployments).
