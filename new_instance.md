# Setting up a new instance

One instance is one project (Camp, Spokane, ...). Each instance gets its own
droplet, Cloudflare Tunnel, hostname, Access application and R2 buckets, so
nothing one instance holds can reach another's. Camp
(`app.vectorannotation.org`) is set up this way.

> **Not tried yet.** Written on 2026-11-25 from Camp's setup and Cloudflare's
> documentation. Nobody has run these steps from start to finish. The API
> commands were checked only offline (shell syntax and JSON bodies), never
> against Cloudflare itself. Run them one at a time, check each answer, and
> correct this file after the first real setup.

The steps below are in the order to do them. Each Cloudflare step shows the
dashboard way and the API way; use either. Values marked **keep** are needed
in a later step, and the table at the end says where each one goes. Never
commit them: this repository is public.

## 0. Before you start

- A DigitalOcean account with your SSH key on it.
- For the API way: a Cloudflare **Account API Token** (it starts with
  `cfat_`). The account owner makes it under Manage Account → Account API
  Tokens → Create Token → Custom token, with an end date (TTL) and:

  | Permission | Resources |
  |---|---|
  | Account › Cloudflare Tunnel › Edit | the account |
  | Account › Access: Apps and Policies › Edit | the account |
  | Zone › DNS › Edit | only `vectorannotation.org` |

  Tunnel and Access rights cover the **whole account**, Camp included, so
  this is a setup key for one person. It never goes on a droplet or into
  git, and it is deleted when the setup is done (step 9).

Shell setup for the API commands, on your own machine (needs `curl` and `jq`).
Both IDs are on the `vectorannotation.org` Overview page, under API:

```bash
read -rsp 'Cloudflare token: ' CF_API_TOKEN && echo && export CF_API_TOKEN   # not saved in shell history
export ACCOUNT_ID=<account id> ZONE_ID=<zone id>
export PROJECT=<project> APP_HOST=<project>.vectorannotation.org
API=https://api.cloudflare.com/client/v4
cf() { curl -sS -H "Authorization: Bearer $CF_API_TOKEN" -H 'Content-Type: application/json' "$@"; }

cf "$API/accounts/$ACCOUNT_ID/tokens/verify" | jq .result.status   # "active"
```

If a command below prints `null` or `false`, run it again without the
`| jq ...` part to see Cloudflare's error message.

## 1. Droplet

Dashboard: DigitalOcean → Create → Droplets. Ubuntu 24.04 (LTS), Basic,
2 vCPU / 4 GB like Camp, a region near the reviewers (Camp: sfo2), SSH key
login. Recommended: a cloud firewall (Networking → Firewalls) with inbound
SSH only and the default outbound rules. The tunnel connects outward, so no
web port has to be open.

API, with `doctl` (DigitalOcean's CLI, after `doctl auth init`):

```bash
doctl compute ssh-key list    # your key's ID
doctl compute droplet create "damagemap-$PROJECT" --image ubuntu-24-04-x64 \
    --size s-2vcpu-4gb --region <region> --ssh-keys <key ID> --wait
```

**Keep** the droplet's IPv4 address: `export DROPLET_IP=<address>`.

## 2. Software on the droplet

On the droplet, as root:

```bash
apt-get update
apt-get install -y docker.io docker-compose-v2 git python3-boto3   # boto3: for the backup script
# 8 GB of swap, as on Camp
fallocate -l 8G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
# cloudflared, from Cloudflare's apt repository (apt upgrade keeps it current)
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
    > /etc/apt/sources.list.d/cloudflared.list
apt-get update && apt-get install -y cloudflared
# The app. The folder name gives the Compose project name, damagemap-qaqc,
# which the backup script looks for.
git clone https://github.com/kulpojke/quick-qaqc-app.git /opt/damagemap-qaqc
```

## 3. Access application

Make it **before** the hostname exists (step 4), so the hostname is never
reachable without a login.

Dashboard: Zero Trust → Access controls → Applications → Create new
application → Self-hosted.

- Name `damagemap <project>`, session duration 24 hours, public hostname
  `<project>.vectorannotation.org`.
- Login method: One-time PIN.
- Policy: action Allow, Include → Emails → every reviewer's address.

After saving, **keep** the app's *Application Audience (AUD) Tag*.

API:

```bash
POLICY_ID=$(cf -X POST "$API/accounts/$ACCOUNT_ID/access/policies" --data @- <<EOF | jq -r .result.id
{"name": "$PROJECT reviewers", "decision": "allow",
 "include": [{"email": {"email": "reviewer1@example.org"}},
             {"email": {"email": "reviewer2@example.org"}}]}
EOF
)
CF_ACCESS_AUD=$(cf -X POST "$API/accounts/$ACCOUNT_ID/access/apps" --data @- <<EOF | jq -r .result.aud
{"type": "self_hosted", "name": "damagemap $PROJECT", "domain": "$APP_HOST",
 "session_duration": "24h", "policies": [{"id": "$POLICY_ID", "precedence": 1}]}
EOF
)
echo "$CF_ACCESS_AUD"   # keep
```

Without `allowed_idps`, the app offers every login method the account has;
today that is only the one-time PIN.

Every address on the policy must also be a reviewer ID in the project YAML,
spelled the same way: the app takes the reviewer from the login's email. An
address that is not on the policy gets **no PIN email at all**. Cloudflare
sends nothing and gives no reason.

## 4. Tunnel and hostname

Dashboard: Networking → Tunnels → Create a tunnel, name `damagemap-<project>`.

1. The next page shows an install command that ends in a long token
   (`eyJ...`). **Keep** only the token. Don't run that command: it puts the
   token on the command line, and we keep it in a file.
2. On the droplet, write the token to the file without a newline:

   ```bash
   (umask 077 && mkdir -p /etc/cloudflared && read -rsp 'Tunnel token: ' TOKEN \
       && printf '%s' "$TOKEN" > /etc/cloudflared/token)
   ```

3. Start the service, below, and wait for the tunnel to show as connected.
4. Continue to Routes → Add route → Published application: subdomain
   `<project>`, domain `vectorannotation.org`, service URL
   `http://localhost:8501`. The dashboard adds the DNS record for you.

API:

```bash
TUNNEL_ID=$(cf -X POST "$API/accounts/$ACCOUNT_ID/cfd_tunnel" --data @- <<EOF | jq -r .result.id
{"name": "damagemap-$PROJECT", "config_src": "cloudflare"}
EOF
)
# Our hostname goes to the app; the last rule, which Cloudflare requires,
# answers anything else with 404.
cf -X PUT "$API/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" --data @- <<EOF | jq .success
{"config": {"ingress": [{"hostname": "$APP_HOST", "service": "http://localhost:8501"},
                        {"service": "http_status:404"}]}}
EOF
cf -X POST "$API/zones/$ZONE_ID/dns_records" --data @- <<EOF | jq .success
{"type": "CNAME", "name": "$APP_HOST", "content": "$TUNNEL_ID.cfargotunnel.com", "proxied": true}
EOF
# The token goes straight onto the droplet, never onto this machine's disk.
cf "$API/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/token" | jq -j .result \
    | ssh "root@$DROPLET_IP" '(umask 077 && mkdir -p /etc/cloudflared && cat > /etc/cloudflared/token)'
```

The service, either way, on the droplet. As on Camp, it reads the token from
the file, so the token doesn't show in `ps` or in the unit file:

```bash
cat > /etc/systemd/system/cloudflared.service <<'EOF'
[Unit]
Description=Cloudflare Tunnel client
After=network-online.target
Wants=network-online.target

[Service]
TimeoutStartSec=15
Type=notify
ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel run --token-file /etc/cloudflared/token
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now cloudflared
journalctl -u cloudflared -n 20   # look for "Registered tunnel connection"
```

## 5. R2 buckets and keys

Do this part in the dashboard, as the account owner. Making buckets through
the API needs R2 rights over the whole account, and those would also reach
Camp's buckets and backups.

R2 Object Storage → Create bucket, twice. Both stay private (public access
off):

- `<project>-exports`: the export worker's Parquet files.
- `<project>-backups`: the nightly database dumps. In its Settings → Object
  lifecycle rules, delete objects after 30 days and abort incomplete
  multipart uploads.

Then R2 Object Storage → Manage API tokens → Create Account API token, once
per bucket: Object Read & Write, Apply to specific buckets only, that one
bucket. **Keep** each Access Key ID and Secret Access Key (shown only once),
and the S3 endpoint `https://<account id>.r2.cloudflarestorage.com`.

Input data: the app reads layers and imagery over HTTPS or from local files,
so inputs in R2 need a public bucket URL today. That is fine for public data
such as DINS. Licensed imagery or data needs more work first.

## 6. Configure and start the app

On the droplet:

```bash
cd /opt/damagemap-qaqc
cp .env.example .env && chmod 600 .env
openssl rand -hex 24    # a database password
```

In `.env`, set:

```dotenv
POSTGRES_PASSWORD=<the password>
AUTH_MODE=cloudflare
# The same for every instance: copy it from Camp's .env.
CF_ACCESS_TEAM_DOMAIN=https://<team>.cloudflareaccess.com
CF_ACCESS_AUD=<AUD tag from step 3>
R2_ACCESS_KEY_ID=<exports bucket key ID>
R2_SECRET_ACCESS_KEY=<exports bucket secret>
```

In the project YAML, the reviewer IDs are the emails on the Access policy,
and `exports` points at the new bucket:

```yaml
exports:
  bucket: <project>-exports
  endpoint_url: https://<account id>.r2.cloudflarestorage.com
  prefix: exports
```

Keep a customer's YAML out of this public repository, for example in
`/opt/<project>-config/`. `run.sh` accepts a path outside the repository, and
relative data paths in the YAML are read from the YAML's own folder. Start the
app with the assignment flags described in the README, under "Automatic hex
assignments" (Camp used `--auto-assign-hex 1 2 --hex-resolution 8`):

```bash
./run.sh <path to the project YAML> <assignment flags>
```

## 7. Nightly backup

On the droplet, the settings for `scripts/backup_db.py`, with the backups
bucket's key from step 5. Type the four lines, then Ctrl-D:

```bash
(umask 077 && cat > /etc/damagemap-backup.env)
```

```dotenv
R2_ENDPOINT=https://<account id>.r2.cloudflarestorage.com
R2_BUCKET=<project>-backups
R2_ACCESS_KEY_ID=<backups bucket key ID>
R2_SECRET_ACCESS_KEY=<backups bucket secret>
```

Run it once by hand, then schedule it. The droplet's clock is UTC, so 11:17
is 03:17 Pacific:

```bash
python3 /opt/damagemap-qaqc/scripts/backup_db.py --instance <project>
echo '17 11 * * * root flock -n /run/damagemap-backup-<project>.lock python3 /opt/damagemap-qaqc/scripts/backup_db.py --instance <project> >> /var/log/damagemap-backup.log 2>&1' \
    > /etc/cron.d/damagemap-backup
```

## 8. Check

From any machine:

```bash
curl -sI "https://$APP_HOST/" | grep -i -E '^(HTTP|location)'   # 302 to <team>.cloudflareaccess.com
```

Then log in, in a browser, with a reviewer's address: the PIN email arrives
and the map loads. On the droplet:

```bash
ss -tlnp | grep -E ':(8501|8000|5432) '   # only 127.0.0.1, never 0.0.0.0
docker ps --filter label=com.docker.compose.project=damagemap-qaqc
tail -4 /var/log/damagemap-backup.log     # the morning after
```

Optional: tile caching. Camp has a Cache Rule (`vectorannotation.org` →
Caching → Cache Rules) so that Cloudflare caches imagery tiles. Add a rule
like it for the new host, in the dashboard (the setup token can't do this):
`(http.host eq "<project>.vectorannotation.org" and starts_with(http.request.uri.path, "/api/cog/tile/"))`,
eligible for cache, Edge TTL from the origin's Cache-Control.

## 9. Clean up

Delete the setup token (Manage Account → Account API Tokens), or let its end
date pass, and run `unset CF_API_TOKEN`.

## What goes where

| Value | From | Goes to |
|---|---|---|
| Setup token (`cfat_...`) | step 0 | only the shell doing the setup; deleted afterwards |
| AUD tag | step 3 | `CF_ACCESS_AUD` in `/opt/damagemap-qaqc/.env` |
| Tunnel token | step 4 | `/etc/cloudflared/token` (mode 600) |
| Team domain | Camp's `.env` | `CF_ACCESS_TEAM_DOMAIN` in `.env` |
| Exports bucket key | step 5 | `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` in `.env` |
| Backups bucket key | step 5 | `/etc/damagemap-backup.env` (mode 600) |
| Reviewer emails | the project | the Access policy (step 3) **and** the YAML's reviewer IDs |
