# Same-host install

Target layout on one machine:

    /home/<user>/model-router/            code (this checkout)
    /home/<user>/model-router-data/       mutable state
    /home/<user>/.config/model-router/    private env (secrets)

## Steps

1. Place the checkout at ~/model-router.
2. scripts/install.sh — creates .venv (python3.14; falls back to `uv venv`
   when ensurepip is unavailable), installs requirements.txt, creates
   data dirs.
3. Private env:

       mkdir -p ~/.config/model-router
       cp .env.example ~/.config/model-router/router.env
       # fill PROVIDER_*_API_KEY, optional tokens; then
       chmod 600 ~/.config/model-router/router.env

4. Install systemd units from deploy/ into ~/.config/systemd/user/
   (see the unit files for Environment/State settings), then:

       systemctl --user daemon-reload
       systemctl --user enable --now model-router.service

5. Verify: scripts/verify.sh 4100

## Clean-room verification (optional, recommended after install)

- fresh venv: .venv created only by install.sh; runtime loads modules from
  the product tree (test: test_runtime_loads_from_product_root)
- fresh state: point GATEWAY_STATE_DIR at an empty dir; the service
  creates control.db, discovers providers, builds the registry, lifecycle
  defaults and policy defaults on first start
- isolation: no reference to any legacy deployment path
  (test_no_source_reference_to_legacy_tree)

## Instances on one host

Set per-unit env: GATEWAY_STATE_DIR (per-instance telemetry),
GW_CONTROL_DB/GW_CONFIG (shared control), GATEWAY_V2_PORT,
MODEL_ROUTER_INSTANCE_ID. Ports must be free; default 4100/4101/4111 are
production/canary/control — use others (e.g. 4210/4211) for testing.
