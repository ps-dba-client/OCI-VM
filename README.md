# OCI-VM — OCI metrics bridge on a Linux VM

Python bridge: **OCI Monitoring** (list + summarize) → **Splunk Observability** (metrics ingest) + **Splunk Cloud** (HEC events).

**Assumption:** You already have a **Linux VM**—for example on **GCP**, **Azure**, **OCI Compute**, another provider, or **on-prem**—with outbound **HTTPS** to OCI and Splunk. This repo does **not** cover creating or sizing that VM; it only covers OCI IAM, installing this bridge under `/opt/script`, and configuration.

The [serverless sample](https://github.com/ps-dba-client/OCI) uses **OCI resource principals**. On a non-OCI or external host you use an **OCI IAM user API key** in `~/.oci/config` and policies that allow **read metrics** for the compartments you query.

**Related (Terraform / Functions / enterprise rollout):** the companion repo documents the **OCI Functions** path, image build, CI, and strict-networking phases: [docs index](https://github.com/ps-dba-client/OCI/tree/main/docs), [Enterprise deployment](https://github.com/ps-dba-client/OCI/blob/main/docs/ENTERPRISE-DEPLOYMENT.md), [Function image deploy](https://github.com/ps-dba-client/OCI/blob/main/docs/DEPLOY-FUNCTION.md), [GitHub & `$HOME/.ssh/id_ed25519_github`](https://github.com/ps-dba-client/OCI/blob/main/docs/DEPLOY-GITHUB.md).

---

## Full setup: OCI + install + run

Step-by-step **OCI IAM** (user, API key, group, policy), **metrics scope** (tenancy/subtree), **copy credentials to the host**, **install**, **`env`**, and **troubleshooting**:

**[docs/VM-END-TO-END-SETUP.md](docs/VM-END-TO-END-SETUP.md)**

---

## Layout (on the VM after install)

| Path in repo | On the VM |
|--------------|-----------|
| `opt/script/oci_metrics_bridge_vm.py` | `/opt/script/oci_metrics_bridge_vm.py` |
| `opt/script/requirements.txt` | `/opt/script/requirements.txt` |
| `opt/script/run-with-otel.sh` | `/opt/script/run-with-otel.sh` |
| `opt/script/env.example` | Copy to `/opt/script/env` (secrets, not committed) |

[`install-to-opt.sh`](install-to-opt.sh) copies files to **`/opt/script/`** and creates the Python **venv** there.

---

## Install on the VM

As **root** on your existing host:

```bash
apt-get update && apt-get install -y git python3 python3-venv rsync
git clone https://github.com/ps-dba-client/OCI-VM.git /usr/local/src/OCI-VM
cd /usr/local/src/OCI-VM
chmod +x install-to-opt.sh
./install-to-opt.sh
```

Then complete **OCI** setup and **`/opt/script/env`** per **[docs/VM-END-TO-END-SETUP.md](docs/VM-END-TO-END-SETUP.md)**.

**Traces / log correlation:** the venv must include **`splunk-opentelemetry`** (installed via `requirements.txt`). Without it, **trace_id / span_id** stay empty. [`run-with-otel.sh`](opt/script/run-with-otel.sh) sets `OTEL_PYTHON_LOG_CORRELATION=true` and runs the app under `opentelemetry-instrument`.

---

## OCI credentials on the VM

Create the IAM user, API key, group, and policy on the OCI side (see the full guide). On the host:

```bash
sudo mkdir -p /root/.oci
sudo chmod 0700 /root/.oci
# Place config + private PEM; chmod 600
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

---

## Configure `/opt/script/env`

```bash
sudo cp /opt/script/env.example /opt/script/env
sudo chmod 600 /opt/script/env
sudo nano /opt/script/env
```

Set Splunk and OCI scope variables as described in the full guide and in [`opt/script/env.example`](opt/script/env.example).

---

## Test

```bash
sudo /opt/script/run-with-otel.sh
echo $?
sudo cat /opt/script/logs/last-run.json
```

**Debug without OTLP** (no distributed traces):

```bash
sudo /opt/script/venv/bin/python /opt/script/oci_metrics_bridge_vm.py
```
(with `env` sourced if you rely on exported variables)

---

## Cron (after validation)

```bash
sudo cp /opt/script/cron.example /etc/cron.d/oci-metrics-bridge
sudo chmod 0644 /etc/cron.d/oci-metrics-bridge
```

Adjust schedule as needed.

---

## Security

- Do **not** commit `/opt/script/env`, API private keys, or Splunk tokens.
- Use least-privilege OCI policies and restrict access to the host.
- For **egress allowlists**, **Vault/secrets**, and **IAM split** patterns aimed at regulated environments, align with [Enterprise deployment (OCI repo)](https://github.com/ps-dba-client/OCI/blob/main/docs/ENTERPRISE-DEPLOYMENT.md) where applicable.

## Disclaimer

Sample code for demos and client adaptation—not production-hardened for secrets storage (use Vault / secret manager for long-term deployments).
