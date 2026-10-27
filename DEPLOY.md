# Deploying Finland News

The app runs on one Hetzner VPS with Docker Compose. Caddy is the only public service. It serves HTTPS on the domain and asks for basic auth before it passes a request to the web app. The database is not published, and the worker runs the schedule in [worker/crontab](worker/crontab).

Run these steps once, in order. After that, deploys are a single command.

## 1. Server

1. Create a CX22-class server (2 vCPU, 4 GB) with Ubuntu LTS and add your SSH key.
2. Create a non-root user with sudo. Disable password logins and root SSH logins in `/etc/ssh/sshd_config`, then reload SSH.
3. In the Hetzner Cloud console, add a firewall that allows TCP 22, 80 and 443. Nothing else. Leave 22 open to all addresses if you use the GitHub Actions deploy (section 6), because the runners' addresses change. Key-only SSH is the protection.
4. Install Docker Engine and the compose plugin from Docker's apt repository. Turn on `unattended-upgrades`.
5. Point an A record (and AAAA if you use IPv6) for your domain at the server. Caddy needs the record in place before its first start, so it can get a certificate.

   Without a domain of your own, use a free DuckDNS subdomain. Sign in at duckdns.org, claim a name such as `finnews`, and set it to the server's IPv4. The server's address is static, so you set it once. `finnews.duckdns.org` is then the `SITE_DOMAIN`, and Caddy gets a normal certificate for it. To move to a real domain later, point its record at the server, change `SITE_DOMAIN`, and deploy again.

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

Each push to `main` deploys by itself once section 6 is set up. To deploy by hand on the server:

```bash
scripts/deploy.sh
```

The script refuses to run if `.env` is missing a required value. It never starts the dev override, because every compose call names `docker-compose.yml` explicitly.

## Things to know

- The web app has no login of its own. Basic auth at Caddy is the only protection, so do not publish port 5000 or 8080 on the server.
- Caddy's basic auth covers `/healthz` too. An external uptime check would need either credentials or a different route. The healthchecks.io pings come from the worker, so they do not need it.
- The schedule runs inside the worker container. Its times are in [worker/crontab](worker/crontab) and are Helsinki time.
- An alternative to basic auth is to serve the app only on a Tailscale address. That is still an open question in the plan; this setup uses basic auth.
- Container logs rotate at 10 MB, keeping three files per container. `docker compose -f docker-compose.yml logs` shows them.

## 5. Backups and monitoring

The backup runs from the server's crontab, not from the worker, because the dump goes through `docker compose`. Each night it dumps the database, checks that the dump can be read back, keeps 14 daily dumps and 8 weekly Sunday dumps, and copies the directory to the Storage Box.

1. Create a Hetzner Storage Box and add the server user's SSH key to it. Then add an alias to `~/.ssh/config` with the Storage Box's host, user and port (23), so rsync can use it by name.
2. Add these to `.env` on the server:

   ```bash
   BACKUP_DIR=/var/backups/finland-news
   BACKUP_REMOTE=storagebox:ft-backups
   HC_BACKUP_URL=https://hc-ping.com/<uuid>
   ```

   `BACKUP_REMOTE` is the rsync destination. The off-site copy uses `rsync --delete`, so it mirrors the local directory exactly. Point it at a directory that holds only these backups.

3. Run the backup once by hand, then check the restore:

   ```bash
   scripts/backup.sh
   scripts/restore-check.sh /var/backups/finland-news/ft-$(date +%F).dump
   ```

4. Add the nightly job with `crontab -e`, as the user that runs Docker:

   ```
   30 2 * * * /home/<user>/finland_news/scripts/backup.sh >> /home/<user>/ft-backup.log 2>&1
   ```

   The server clock is UTC by default. 02:30 UTC is 04:30 in Helsinki in winter and 05:30 in summer. Either is fine.

5. In healthchecks.io, create three checks:
   - **run**: period 12 hours, grace 1 hour. Its URL is `HC_FETCH_URL`, which the pipeline pings.
   - **digest**: period 1 day, grace 1 hour. Its URL is `HC_DIGEST_URL`.
   - **backup**: period 1 day, grace 1 hour. Its URL is `HC_BACKUP_URL`. The script pings `/fail` when any step fails.

6. Turn on weekly server snapshots in the Hetzner console (the backups add-on, or manual snapshots). They are not automated here.

Run `scripts/restore-check.sh` again after any schema change. It restores into a scratch database called `ft_restore_check`, then drops that database.

## 6. Continuous deployment

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs lint and the tests on every push and pull request, against a pgvector Postgres. [.github/workflows/deploy.yml](.github/workflows/deploy.yml) runs the same checks on each push to `main`, then connects to the server over SSH and runs `scripts/deploy.sh`. A failed `/healthz` check fails the workflow. To deploy again without a push, run the Deploy workflow from the Actions tab with `main` selected. A run from any other branch runs CI only.

Do the first deploy by hand (section 3) before you set this up.

1. On your laptop, create a key that is used only by GitHub:

   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/finland_news_ci -N "" -C github-actions
   ```

2. On the server, add the public key to `~/.ssh/authorized_keys` of the deploy user, with a forced command in front of it:

   ```
   restrict,command="cd ~/finland_news && scripts/deploy.sh" ssh-ed25519 AAAA... github-actions
   ```

   Whatever command the client asks for, this key only runs the deploy. `restrict` turns off the shell, port forwarding and agent forwarding.

3. Get the server's host key, so the workflow can check it:

   ```bash
   ssh-keyscan -t ed25519 <server-ip>
   ```

4. In GitHub, open Settings → Environments, create an environment called `production`, and add these secrets:
   - `SSH_HOST`: the server's IP address.
   - `SSH_USER`: the deploy user.
   - `SSH_PRIVATE_KEY`: the contents of `~/.ssh/finland_news_ci`.
   - `SSH_KNOWN_HOSTS`: the `ssh-keyscan` output.

   To approve each deploy by hand, add yourself as a required reviewer on the environment.

5. Check that the key cannot do anything else. This should run a deploy, not `ls`:

   ```bash
   ssh -i ~/.ssh/finland_news_ci <user>@<server-ip> ls
   ```
