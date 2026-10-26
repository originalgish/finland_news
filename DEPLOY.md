# Deploying Finland News

The app runs on one Hetzner VPS with Docker Compose. Caddy is the only public service. It serves HTTPS on the domain and asks for basic auth before it passes a request to the web app. The database is not published, and the worker runs the schedule in [worker/crontab](worker/crontab).

Run these steps once, in order. After that, deploys are a single command.

## 1. Server

1. Create a CX22-class server (2 vCPU, 4 GB) with Ubuntu LTS and add your SSH key.
2. Create a non-root user with sudo. Disable password logins and root SSH logins in `/etc/ssh/sshd_config`, then reload SSH.
3. In the Hetzner Cloud console, add a firewall that allows TCP 22 (optionally only from your IP), 80 and 443. Nothing else.
4. Install Docker Engine and the compose plugin from Docker's apt repository. Turn on `unattended-upgrades`.
5. Point an A record (and AAAA if you use IPv6) for your domain at the server. Caddy needs the record in place before its first start, so it can get a certificate.

## 2. Code and secrets

1. Give the server read access to the repository: a read-only deploy key on GitHub, or an HTTPS clone.
2. Clone the repository into the user's home directory.
3. Create `.env` from `.env.example`. Fill in the database password, `ANTHROPIC_API_KEY`, and the healthchecks.io URLs. Then set the production values:

   ```bash
   COMPOSE_FILE=docker-compose.yml
   COMPOSE_PROFILES=prod
   SITE_DOMAIN=news.example.com
   BASIC_AUTH_USER=denis
   BASIC_AUTH_HASH='$2a$14$...'
   ```

   `COMPOSE_FILE` keeps the dev override from loading. That file publishes the database and the web app, and the server must not publish either.

4. Generate the hash on the server, and copy only the hash into `.env`:

   ```bash
   docker run --rm -it caddy:2 caddy hash-password
   ```

   Keep the single quotes around the hash. It contains `$` signs.

## 3. First deploy

```bash
scripts/deploy.sh
```

The script checks that the required values are in `.env`, pulls `main`, builds the images, runs the migrations, starts the containers, and checks that the web app answers `/healthz`.

Then check the site from a browser. It should ask for the basic-auth login, then show the feed. The first certificate takes a minute.

## 4. Deploys after that

```bash
scripts/deploy.sh
```

The script refuses to run if `.env` is missing a required value. It never starts the dev override, because every compose call names `docker-compose.yml` explicitly.

## Things to know

- The web app has no login of its own. Basic auth at Caddy is the only protection, so do not publish port 5000 or 8080 on the server.
- Caddy's basic auth covers `/healthz` too. An external uptime check would need either credentials or a different route. The healthchecks.io pings come from the worker, so they do not need it.
- The schedule runs inside the worker container. Its times are in [worker/crontab](worker/crontab) and are Helsinki time.
- An alternative to basic auth is to serve the app only on a Tailscale address. That is still an open question in the plan; this setup uses basic auth.
- Backups are not set up yet. That is Phase 10.
