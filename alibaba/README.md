# Deploying STADO×Qwen on Alibaba Cloud ECS

The demo is a single Docker container (dual PX4 SITL + GCS backend + nginx)
plus a Caddy sidecar for automatic HTTPS. It needs **dedicated CPU cores** —
two Gazebo physics servers starve on shared-scheduler platforms (learned the
hard way on serverless: sensor watchdogs fire, arming gets refused). A
4-vCPU / 16 GB ECS instance runs it comfortably.

## 1. Provision the instance

Console → ECS → Create Instance:

| Setting | Value |
|---|---|
| Region | `ap-southeast-1` (Singapore — same region as the Model Studio intl endpoint, keeps voice RTT low) |
| Instance type | `ecs.c7.xlarge` (4 vCPU, 8 GB) or `ecs.g7.xlarge` (4 vCPU, 16 GB) — **x86_64, dedicated** (not burstable `t`-series) |
| Image | Ubuntu 22.04 / 24.04 64-bit |
| Disk | 60 GB ESSD (image + build cache) |
| Public IP | Assign (or bind an EIP so the IP survives stop/start) |

Security group inbound rules:

| Port | Purpose |
|---|---|
| 22/tcp | SSH (restrict to your IP) |
| 80/tcp | HTTP (Caddy — ACME challenge + redirect) |
| 443/tcp | HTTPS (the demo) |

## 2. Install Docker

```bash
ssh root@<ECS_IP>
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

## 3. Get the code + configure

```bash
git clone https://github.com/<you>/stado-qwen.git /opt/stado-qwen
cd /opt/stado-qwen
cp .env.example .env
vi .env    # set DASHSCOPE_API_KEY (+ QWEN_WS_URL for intl workspace accounts,
           #  VITE_GOOGLE_MAPS_API_KEY for the map)
```

The `.env` stays on the VM only — it is git-ignored and docker-ignored;
secrets reach the container exclusively as `docker run` env vars.

## 4. DNS (only if you want a hostname + TLS)

Point an A record (e.g. `stado-qwen.example.com`) at the instance's public
IP **before** the first deploy, so Caddy's Let's Encrypt issuance succeeds
on the first try (its ACME retry backoff is exponential — if you point DNS
late, `docker restart caddy` to force an immediate retry).

## 5. Deploy

```bash
./alibaba/deploy.sh                          # plain HTTP on :80 (fastest for judging)
DOMAIN=stado-qwen.example.com ./alibaba/deploy.sh   # HTTPS via Caddy + Let's Encrypt
```

The script builds the image on the box (native x86 — no cross-arch push),
replaces the running container, and waits for `/api/ready` (200 = both SITL
drones MAVLink-connected) before printing the URL.

Redeploy after a code change = `git pull && ./alibaba/deploy.sh` again.

## 6. Ops notes

- **Restart cadence**: the gz_x500 sim battery depletes after ~30 min of
  being armed; the relax-preflight script disables the battery failsafes,
  but a periodic restart keeps the sim fresh anyway:
  ```bash
  (crontab -l 2>/dev/null; echo "0 */6 * * * docker restart stado-qwen") | crontab -
  ```
- **Logs**: `docker logs -f stado-qwen` — PX4 lines are prefixed
  `[px4-overwatch]` / `[px4-outrider]`, backend `[backend]`.
- **Health**: `curl localhost:8080/_health` (nginx), `curl localhost:8080/api/ready`
  (SITL link state).
- **Cost control**: stop the instance when not demoing; with an EIP the IP
  (and DNS) survive. The hackathon credits cover a c7.xlarge for the
  judging window comfortably.
