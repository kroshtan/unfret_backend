# Production cut-over runbook (one-time)

Until go-live, `unfret.kroshtan.com` is still served by the **pre-split** single deployment: the Kamal service
`veridex` with its database `veridex-db`, on **devnet**. Since the dev/production split, nothing deploys to it any
more. Pushes to `main` go to `veridex-dev` (dev.unfret.kroshtan.com), and production (`veridex-prod`) has never been
deployed. This runbook moves `unfret.kroshtan.com` from the old service to `veridex-prod` on mainnet.

kamal-proxy refuses to give a host to a second service, so the old service's route has to go first. Expect **about
1–3 minutes of downtime**: from removing the old route until `veridex-prod` passes its readiness check (including
pulling the image and booting `veridex-prod-db` the first time).

## Before

- [ ] **Repo secrets set** (Settings → Actions → Secrets):
  - `PROD_POSTGRES_PASSWORD`, `PROD_SESSION_SECRET`, `PROD_HOT_WALLET_KEY`, `PROD_COLD_ADDRESS`
  - `PROD_SOLANA_RPC_URL`: a mainnet RPC provider
  - `VERIDEX_LLM_API_KEY`
- [ ] **The mainnet hot wallet is funded:** `GzcZjcsVr6XGypgEaBxtHHgqKMbMqXN5QBfZ3rNDPYnL`, ≥ 0.02 SOL (the reserve plus
  two token-account rents).
- [ ] **The release tested on dev:** the release commit is on `main`, deployed and tested on dev.unfret.kroshtan.com.
- [ ] **Tagged:** `git tag v0.6.0 <sha> && git push origin v0.6.0`.
- [ ] **Backups armed:** `/etc/forge/backup.env`. The production database holds customers' balances from the first
  purchase on.

## Cut-over (on the server, as `admin` with sudo)

1. **See what's routed.** The old route is `veridex-web`:
   ```bash
   sudo docker exec kamal-proxy kamal-proxy list
   ```
2. **Take the old service off the host** (downtime starts), then stop, but **don't remove**, its containers so you can
   roll back:
   ```bash
   sudo docker exec kamal-proxy kamal-proxy remove veridex-web
   sudo docker ps --filter label=service=veridex --format '{{.ID}} {{.Names}}'   # note the names for rollback
   sudo docker stop $(sudo docker ps -q --filter label=service=veridex)
   ```
   This stops both `veridex-web-<sha>` and `veridex-db`.
3. **Deploy production.** In Forgejo: **Actions → Deploy production → Run workflow**, `ref` = the tag. It boots
   `veridex-prod-db` (empty) and deploys `veridex-prod` with the image dev already ran. kamal-proxy routes
   `unfret.kroshtan.com` to it once `/v1/health/ready` passes (downtime ends).
4. **Verify:**
   ```bash
   curl -s https://unfret.kroshtan.com/v1/health/ready                # ready; x402 ok, credits ok, no alerts
   curl -s https://unfret.kroshtan.com/v1/pricing | head -c 300       # network solana-mainnet
   curl -si -X POST https://unfret.kroshtan.com/v1/check -H 'Content-Type: application/json' \
     -d '{"url":"https://example.com","tier":"quick","policy":"balanced"}' | head -3   # 402, mainnet network id
   ```
   Within a minute, the sweep job creates the hot wallet's wSOL and USDC accounts (a rent of about 0.004 SOL in
   total).

## Rollback (before the clean-up below)

```bash
sudo docker exec kamal-proxy kamal-proxy remove veridex-prod-web
sudo docker stop $(sudo docker ps -q --filter label=service=veridex-prod)
sudo docker start veridex-db <veridex-web-container-name-from-step-2>
sudo docker exec kamal-proxy kamal-proxy deploy veridex-web \
  --target <veridex-web-container-name>:8080 --host unfret.kroshtan.com --health-check-path /v1/health/ready
```

If a production *release* misbehaves later, roll back by running **Deploy production** with the previous tag.

## Clean-up (after a few quiet days)

- **Remove the old containers:**
  ```bash
  sudo docker rm $(sudo docker ps -aq --filter label=service=veridex)
  ```
  Then archive or delete `/home/kamal/veridex-db/`, the devnet data from before the split.
- **forge-infra:** drop `veridex-db:veridex:veridex` from `APP_PG` in `backup/forge-backup.sh`, then run bootstrap.
- **Delete the pre-split repo secrets:** `POSTGRES_PASSWORD`, `VERIDEX_SESSION_SECRET`, `VERIDEX_HOT_WALLET_KEY`,
  `VERIDEX_COLD_ADDRESS`, `VERIDEX_TINEYE_API_KEY`.
