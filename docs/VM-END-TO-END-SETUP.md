# OCI metrics bridge: OCI prerequisites + running on your VM

This guide focuses on **what to configure in OCI** and **how to install and run** the bridge on a **Linux VM you already have** (on-prem, cloud, or OCI Compute). It does **not** cover provisioning that VM.

The bridge uses an **OCI IAM user API key** (not OCI resource principals). Outbound **HTTPS** from the VM to OCI Monitoring and Splunk is required.

> **Enterprise networking / change control:** Firewall allowlists, least-privilege IAM, secrets handling, and phased rollout guidance for regulated environments are summarized in the companion repo: [Enterprise deployment (`ps-dba-client/OCI`)](https://github.com/ps-dba-client/OCI/blob/main/docs/ENTERPRISE-DEPLOYMENT.md). That doc targets **OCI Functions**, but many checklist items apply to this **VM** bridge as well (egress destinations, token storage, who owns IAM).

---

## 1. What you are building

| Piece | Role |
|--------|------|
| **OCI IAM user** | Identity the Python SDK uses to sign Monitoring API calls (`list_metrics`, `summarize_metrics_data`). |
| **API key** | Private key on the VM + public key on that user; `~/.oci/config` points at the PEM. |
| **IAM policy** | Lets that user **read metrics** in a compartment, the whole **tenancy**, or a subtree (see §4). |
| **`/opt/script/env`** | Splunk Observability + Splunk Cloud HEC + `METRICS_COMPARTMENT_OCID` and tuning. |
| **`splunk-opentelemetry` (Python)** | Required in the venv so `opentelemetry-instrument` installs a **real** `TracerProvider`. Without it, spans are no-op and **trace_id / span_id stay empty** in logs and HEC. |

---

## 2. Prerequisites

**On OCI (or via admin):**

- Tenancy where metrics should be read; permission to create users, groups, and policies (or someone who can do §3–5 for you).
- **OCI CLI** on a **workstation** or **OCI Cloud Shell**, authenticated (e.g. `oci session authenticate` → use `--profile NAME` and `--auth security_token` on IAM commands).

**On the VM (already running):**

- A recent **Ubuntu/Debian**-style OS (or equivalent with `python3`, `venv`, `git`).
- Outbound **HTTPS** to OCI and Splunk endpoints.
- **Root** or **sudo** to install under `/opt/script` (paths in this repo assume that layout).

**Splunk:**

- **Observability:** realm (e.g. `us1`) and **ingest access token**.
- **Splunk Cloud (optional):** HEC URL and token for log events.

---

## 3. OCI: create a dedicated IAM user (recommended)

Use a **technical user** (not a personal admin) with least privilege.

### 3.1 Identity domains and email

Many tenancies use **identity domains**. Creating a user often **requires a primary email**:

```bash
oci iam user create \
  --name oci-metrics-bridge \
  --description "Metrics bridge (external host)" \
  --email "oci-metrics-bridge@your-org.example" \
  --profile YOUR_ADMIN_PROFILE \
  --auth security_token \
  --region YOUR_HOME_REGION
```

- Replace `YOUR_HOME_REGION` (e.g. `us-ashburn-1`) with the region you use for IAM API calls.
- If your tenancy does **not** require email, `--email` may be optional.

**Console:** Identity → Users → Create user (note the **user OCID**).

### 3.2 API signing key pair (workstation)

```bash
mkdir -p ~/.oci
openssl genrsa -out ~/.oci/oci_metrics_bridge_key.pem 2048
openssl rsa -pubout -in ~/.oci/oci_metrics_bridge_key.pem -out ~/.oci/oci_metrics_bridge_key_public.pem
chmod 600 ~/.oci/oci_metrics_bridge_key.pem
```

Upload the **public** key to the user:

```bash
USER_OCID="ocid1.user.oc1..aaaaaaaaREPLACE"

oci iam user api-key upload \
  --user-id "$USER_OCID" \
  --key-file ~/.oci/oci_metrics_bridge_key_public.pem \
  --profile YOUR_ADMIN_PROFILE \
  --auth security_token \
  --region YOUR_HOME_REGION
```

Save the **`fingerprint`** from the JSON output.

**Console:** User → API keys → Add API key.

---

## 4. OCI: IAM policy (read metrics)

The bridge needs permission to **list and read metrics** for the compartment set in `METRICS_COMPARTMENT_OCID` (and subtree, if enabled).

### 4.1 Prefer a **group** in policies (identity domains)

Many domain-enabled tenancies only accept **`ALLOW GROUP ...`**, not `ALLOW USER ...`.

**A. Create a group** (compartment is usually the **tenancy** for cloud groups):

```bash
TENANCY_OCID="ocid1.tenancy.oc1..aaaaaaaaREPLACE"

oci iam group create \
  --compartment-id "$TENANCY_OCID" \
  --name oci-metrics-bridge-operators \
  --description "Can read OCI Monitoring metrics for the metrics bridge"
```

**B. Add the technical user to the group:**

```bash
GROUP_OCID="ocid1.group.oc1..aaaaaaaaREPLACE"

oci iam group add-user \
  --user-id "$USER_OCID" \
  --group-id "$GROUP_OCID" \
  --profile YOUR_ADMIN_PROFILE \
  --auth security_token \
  --region YOUR_HOME_REGION
```

**C. Policy statements** (attach in the tenancy or your policy compartment):

- **Entire tenancy** (typical for “all metrics” with subtree from tenancy OCID):

  ```text
  ALLOW GROUP oci-metrics-bridge-operators TO READ METRICS IN TENANCY
  ```

- **Single compartment:**

  ```text
  ALLOW GROUP oci-metrics-bridge-operators TO READ METRICS IN COMPARTMENT id ocid1.compartment.oc1..aaaaaaaaREPLACE
  ```

**Create policy (CLI example):**

```bash
oci iam policy create \
  --compartment-id "$TENANCY_OCID" \
  --name oci-metrics-bridge-read-metrics \
  --description "Metrics bridge can read metrics" \
  --statements '["ALLOW GROUP oci-metrics-bridge-operators TO READ METRICS IN TENANCY"]' \
  --profile YOUR_ADMIN_PROFILE \
  --auth security_token \
  --region YOUR_HOME_REGION
```

If **`NotAuthorizedOrNotFound`** appears on `list_metrics`: confirm the user is in the group, the statement matches your naming, the policy is in the right compartment, and wait a few seconds after changes.

---

## 5. OCI config + private key on the VM

Build a file the OCI SDK expects. **Do not commit** this file or the PEM to git.

**Example `/root/.oci/config`:**

```ini
[DEFAULT]
user=ocid1.user.oc1..aaaaaaaaREPLACE
fingerprint=aa:bb:cc:dd:...
tenancy=ocid1.tenancy.oc1..aaaaaaaaREPLACE
region=us-ashburn-1
key_file=/root/.oci/oci_metrics_bridge_key.pem
```

- **`region`:** Usually the tenancy home region; telemetry calls use that region’s Monitoring endpoint for the configured scope.
- **`key_file`:** Absolute path to the **private** PEM on the VM.

**Transfer from your workstation** (use your normal method), for example:

```bash
scp ~/.oci/oci_metrics_bridge_key.pem ./oci_config_for_vm user@VM_HOST:/tmp/
```

**On the VM:**

```bash
sudo mkdir -p /root/.oci
sudo mv /tmp/oci_metrics_bridge_key.pem /root/.oci/
sudo mv /tmp/oci_config_for_vm /root/.oci/config
sudo chmod 700 /root/.oci
sudo chmod 600 /root/.oci/config /root/.oci/oci_metrics_bridge_key.pem
```

If the job runs as a non-root user, put the files under that user’s home and set `OCI_CONFIG_FILE` in `/opt/script/env` accordingly.

Keep an **offline backup** of the PEM and config in a secure place for rebuilds.

---

## 6. Scope: “all metrics” (tenancy + subtree)

The bridge lists metric **definitions** and summarizes a **time window** per definition, up to a **per-run cap**.

| Variable | Purpose |
|----------|---------|
| `METRICS_COMPARTMENT_OCID` | Start of the hierarchy. Use the **tenancy OCID** to cover the full tenancy. |
| `LIST_METRICS_IN_SUBTREE` | Set to `true` to include **child** compartments (needed for tenancy-root coverage). |
| `MAX_METRICS_PER_INVOKE` | Max definitions processed **per run** (default in `env.example` is often `75`). Increase for broader coverage; more API usage and runtime. |
| `OCI_METRICS_WINDOW_MINUTES` | Summarization window (e.g. `5`). “No datapoints in window” usually means quiet metrics or a narrow window—not IAM failure. |

**Example (broad tenancy scope):**

```bash
export METRICS_COMPARTMENT_OCID="ocid1.tenancy.oc1..aaaaaaaaREPLACE"
export LIST_METRICS_IN_SUBTREE="true"
export MAX_METRICS_PER_INVOKE="200"
```

IAM must allow **read metrics** for every compartment whose metrics you list; **`READ METRICS IN TENANCY`** is the simplest match for tenancy OCID + subtree.

---

## 7. Install the bridge on the VM

On the VM as **root** (or `sudo`):

```bash
apt-get update && apt-get install -y git python3 python3-venv rsync
git clone https://github.com/ps-dba-client/OCI-VM.git /usr/local/src/OCI-VM
cd /usr/local/src/OCI-VM
chmod +x install-to-opt.sh
./install-to-opt.sh
```

[`install-to-opt.sh`](../install-to-opt.sh) copies scripts to **`/opt/script`**, creates a **venv**, runs **`pip install -r requirements.txt`** (including **`splunk-opentelemetry`**), and verifies that package is present.

**Check:**

```bash
/opt/script/venv/bin/python -c "import importlib.metadata as m; print('splunk-opentelemetry', m.version('splunk-opentelemetry'))"
```

---

## 8. Configure `/opt/script/env`

```bash
cp /opt/script/env.example /opt/script/env
chmod 600 /opt/script/env
nano /opt/script/env
```

Set at minimum:

- `SPLUNK_REALM`, `SPLUNK_ACCESS_TOKEN`
- `SPLUNK_HEC_URL`, `SPLUNK_HEC_TOKEN` (if using HEC)
- `METRICS_COMPARTMENT_OCID`, `LIST_METRICS_IN_SUBTREE` (see §6)
- Adjust `OCI_CONFIG_FILE` / `OCI_CONFIG_PROFILE` if not using `/root/.oci/config` + `DEFAULT`

---

## 9. Run and verify

**Recommended (OTLP traces + log correlation):**

```bash
sudo /opt/script/run-with-otel.sh
echo $?
sudo cat /opt/script/logs/last-run.json
```

- **Splunk Observability:** service **`oci-metrics-bridge-vm`**, spans such as `oci.monitoring.list_metrics`, `splunk_o11y.ingest_datapoints`, `splunk_cloud.hec_submit`.
- **Splunk Cloud:** HEC events should include **`trace_id` / `span_id`** when `splunk-opentelemetry` is installed and you use **`run-with-otel.sh`**.
- **stderr:** after the root span starts, log lines should show non-empty `trace_id` / `span_id`.

**Debug only (no OTLP traces):**

```bash
sudo bash -lc 'set -a; source /opt/script/env; set +a; /opt/script/venv/bin/python /opt/script/oci_metrics_bridge_vm.py'
```

**Optional — Splunk OpenTelemetry Collector on the host:** If your org uses a host collector, install it per Splunk’s documentation; the Python script can still export OTLP directly using `SPLUNK_REALM` and `SPLUNK_ACCESS_TOKEN` as configured in `run-with-otel.sh`.

---

## 10. Troubleshooting

| Symptom | Likely cause | What to do |
|---------|----------------|------------|
| `NotAuthorizedOrNotFound` on `list_metrics` | IAM | User in group? Policy **`READ METRICS`** for the listed scope? Policy propagation delay? |
| Empty **`trace_id` / `span_id`** | Missing distro | `pip install -r /opt/script/requirements.txt`; confirm `splunk-opentelemetry` (§7). Use **`run-with-otel.sh`**. |
| Many “no datapoints in window” | Window / idle metrics | Widen `OCI_METRICS_WINDOW_MINUTES` or retry later. |
| Stale HEC errors in Splunk | Old runs | Filter by recent `_time`. |
| `oci: command not found` on VM | Expected | Bridge uses the **Python SDK**, not the OCI CLI. |

---

## 11. Security

- Least-privilege IAM; dedicated user; rotate API keys on a schedule.
- `chmod 600` on `~/.oci/config`, private PEM, and `/opt/script/env`.
- Do not commit secrets; use a secret manager for production tokens.

---

## 12. See also

- [README.md](../README.md) — layout, cron, quick reference.
- [`opt/script/env.example`](../opt/script/env.example) — environment variables.
