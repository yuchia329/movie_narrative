# Terraform — recap.yuchia.dev production deploy

Deploys the movie-recap platform onto the existing **k3s/ARM EC2** (`hubstream`) and wires
the cloud bits around it, in one `terraform apply`:

| Provider | Manages |
|---|---|
| `aws` | S3 artifact bucket + **lifecycle retention** (the scheduled cleanup) + CORS + a scoped IAM user/key |
| `cloudflare` | `recap.yuchia.dev` A record (proxied → edge TLS) |
| `kubernetes` | `recap` namespace + the `jieshuo-secrets` / `gpu-ssh-key` Secrets + the Grafana dashboard ConfigMap (in `monitoring`) |
| `kustomization` | the app workloads + observability CRs by applying `../k8s/overlays/prod` (api, worker, postgres, redis, gpu-tunnel, ingress, **ServiceMonitors + PrometheusRule**) |

**Out of scope (by design):** the physical GPU box (`nlp-gpu-01.be.ucsc.edu`). Terraform
can't provision hardware — run gpud there once (Part C below). The in-cluster `gpu-tunnel`
connects to it at runtime.

## Prerequisites
- `terraform >= 1.5`, `docker` (on an arm64 host — Apple Silicon or the box itself), `kubectl`.
- AWS credentials in your environment (`AWS_PROFILE` or `AWS_ACCESS_KEY_ID`/`…SECRET…`).
- A Cloudflare API token with **DNS edit** on the `yuchia.dev` zone, and the zone id.
- The GPU box SSH **private key** (the same one `video-search` uses).

## 0. Point kubectl/Terraform at the k3s API (SSH tunnel)
The k3s cert isn't valid for the public IP, so forward the API over SSH and use a local kubeconfig:
```bash
ssh -fN -L 6443:localhost:6443 hubstream
scp hubstream:/etc/rancher/k3s/k3s.yaml ~/.kube/recap-k3s.yaml   # server is already https://127.0.0.1:6443
KUBECONFIG=~/.kube/recap-k3s.yaml kubectl get ns                  # sanity check
```

## 1. Build the ARM image and load it into k3s
```bash
IMAGE_TAG=$(git rev-parse --short HEAD) bash ../../scripts/build_and_load.sh
export TF_VAR_image_tag=$(git rev-parse --short HEAD)
```

## 2. Configure
```bash
cp terraform.tfvars.example terraform.tfvars     # fill it in (gitignored)
export TF_VAR_gpu_ssh_private_key="$(cat ~/.ssh/your_gpu_key)"   # keep keys out of files
```

## 3. Apply
```bash
terraform init
terraform plan      # review: S3 bucket+lifecycle, IAM user, Cloudflare record, ns/secrets, ~8 k8s resources
terraform apply
```

## Part C — gpud on the GPU box (one-time, separate)
```bash
scp -r ../../server ../../jieshuorpc nlp-gpu-01.be.ucsc.edu:~/jieshuo/
ssh nlp-gpu-01.be.ucsc.edu 'bash ~/jieshuo/server/setup_gpu.sh'
# run gpud under systemd (Restart=always) with GPUD_PORT_RANGE=50060-50099 + HF_TOKEN + TTS voice
# — see server/README_deploy.md
```

## Verify
```bash
kubectl -n recap get pods,ingress
dig recap.yuchia.dev +short
open https://recap.yuchia.dev          # upload a short clip; front half + "play original" work via S3
kubectl -n recap logs deploy/gpu-tunnel   # forwards up; ASR/TTS stages complete
# Prometheus → Status/Targets shows recap-api + recap-gpud UP; Grafana has "Recap · Overview".
```

## Notes
- **Footprint:** the box is 2 vCPU / 8 GB shared with monitoring + hubstream + video-search
  (~4.9 GB free at last check). recap is right-sized to ~2 GB. Watch `kubectl top node` after
  apply; if tight, the pushgateway is already off and you can bump to t4g.xlarge.
- **Image tag:** `var.image_tag` is string-substituted into the rendered `recap:latest` so a
  unique tag (git sha) forces a rollout. Re-run step 1 + `terraform apply` to ship a new build.
- **Secrets** live only in `terraform.tfvars` / `TF_VAR_*` + Terraform state — never in git or
  the kustomize manifests (the overlay deletes the placeholder Secrets).
- **Alternative to the kustomization provider:** `kubectl apply -k ../k8s/overlays/prod` after
  `terraform apply` has created the namespace + secrets.
