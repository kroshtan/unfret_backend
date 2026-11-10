# Rename cut-over runbook (one-time)

Until the rename, both environments ran as their own Kamal services: dev as `veridex-dev` (database
`veridex-dev-db`) and production as `veridex-prod` (database `veridex-prod-db`, mainnet balances). From the rename on
both are the one service `unfret`: kamal-proxy services `unfret-web-dev` and `unfret-web-production`, databases
`unfret-dev-db` and `unfret-prod-db` (user and database `unfret`), and the image `kroshtan/unfret`. The old
containers were named after the old service; the rename gives every container, proxy route and data directory a new
name, so each destination moves over once.

kamal-proxy refuses to give a host to a second service, so the old route has to go first. Expect **a few minutes of
downtime** per destination: from removing the old route until the new containers pass their readiness check
(including booting the new database and copying the old one into it).

## Before

- [ ] **Repo secret `LLM_API_KEY`** set (Settings → Actions → Secrets), with the value of the old shared LLM key. The
  `DEV_*` and `PROD_*` secrets keep their names.
- [ ] **forge-infra:** `APP_PG` in `backup/forge-backup.sh` names the new databases (`unfret-dev-db:unfret:unfret`,
  `unfret-prod-db:unfret:unfret`), bootstrap run. Keep the old entries until the clean-up below.
- [ ] **A fresh backup of `veridex-prod-db`** (the nightly one, or `pg_dump` by hand).
- [ ] **The version bumped** (`make bump`): the production workflow tags `v<version>` and refuses a tag that already
  names another commit.

## Cut-over

Both deploy jobs run the same two scripts around `kamal accessory boot`; on later deploys they do nothing.

1. **dev, automatically on merge** (`.forgejo/workflows/ci.yaml`, job `deploy-dev`):
   - `scripts/deploy/free-host.sh dev.unfret.kroshtan.com unfret-web-dev` finds the host routed to
     `veridex-dev-web-dev`, removes that route and stops (doesn't remove) the `veridex-dev` web containers;
   - `kamal accessory boot db -d dev` starts `unfret-dev-db` (empty);
   - `scripts/deploy/adopt-db.sh veridex-dev-db unfret-dev-db` pipes `pg_dump --no-owner --no-acl` of the old database
     into the new one in one transaction and stops (doesn't remove) `veridex-dev-db`;
   - `kamal deploy -d dev` starts `unfret-web-dev-<sha>`, and kamal-proxy routes the host to it once it's ready.
2. **production, by hand:** **Actions → Deploy production → Run workflow**, `ref` = `main`. The same steps for
   `unfret.kroshtan.com` / `unfret-web-production`, `veridex-prod-db` → `unfret-prod-db`, then
   `kamal deploy -d production --skip-push --version <sha>` with the image dev ran. With `--require-data`, the copy
   step fails rather than start production on an empty database.
3. **Verify** each destination:
   ```bash
   curl -s https://unfret.kroshtan.com/v1/health/ready                # ready; x402 ok, credits ok, no alerts
   curl -s https://unfret.kroshtan.com/v1/pricing | head -c 300       # network solana-mainnet
   ssh admin@git.kroshtan.com sudo docker exec unfret-prod-db psql -U unfret -d unfret -tAc \
     "SELECT count(*), sum(balance_usd_micros) FROM wallets"          # the balances from before
   ```

If a step fails, run the workflow again: the route is already off, the old app already stopped, and a copy that failed
left the new database empty, so it is retried.

## Rollback (before the clean-up below)

Releases from before the rename can't be deployed by the workflow (their config names the old services). By hand, on
the server as `admin`, for production (dev alike with `dev`, `veridex-dev` and `dev.unfret.kroshtan.com`):

```bash
sudo docker exec kamal-proxy kamal-proxy remove unfret-web-production
sudo docker stop $(sudo docker ps -q --filter label=service=unfret --filter label=destination=production) unfret-prod-db
sudo docker start veridex-prod-db $(sudo docker ps -aq --filter label=service=veridex-prod --filter label=role=web | head -1)
sudo docker exec kamal-proxy kamal-proxy deploy veridex-prod-web-production \
  --target <that container's name>:8080 --host unfret.kroshtan.com --health-check-path /v1/health/ready
```

Anything written to `unfret-prod-db` after the cut-over (purchases, checks) is not in `veridex-prod-db`: settle it from
the new database before rolling back.

## Clean-up (after a few quiet days)

- **Remove the old containers:**
  ```bash
  sudo docker rm $(sudo docker ps -aq --filter label=service=veridex-dev) veridex-dev-db
  sudo docker rm $(sudo docker ps -aq --filter label=service=veridex-prod) veridex-prod-db
  ```
  Then archive or delete `/home/kamal/veridex-dev-db/` and `/home/kamal/veridex-prod-db/`.
- **forge-infra:** drop the old databases from `APP_PG` in `backup/forge-backup.sh`, then run bootstrap.
- **Delete the old repo secret** with the shared LLM key (now `LLM_API_KEY`).
