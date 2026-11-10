# Production runs on the home k8s cluster (deploy/k8s, synced by Argo CD), with the pages on
# Cloudflare (frontend/). Pushing to main deploys both.

.PHONY: dev test logs password-hash uploads-to-r2

dev:     ## run locally with its own Postgres at http://localhost:8000 (no login; uploads kept in Postgres)
	docker compose -f backend/compose.yml up --build

test:    ## needs Docker: starts a throwaway Postgres on 127.0.0.1:5433 the first time
	@docker start easydocs-test-pg >/dev/null 2>&1 || { docker run -d --name easydocs-test-pg -p 127.0.0.1:5433:5432 \
		-e POSTGRES_PASSWORD=test postgres:18.4 >/dev/null && sleep 4; }
	cd backend && uv run pytest -q

logs:    ## tail the API's logs on the cluster
	kubectl -n easydocs logs deploy/easydocs -f --tail 100

password-hash:  ## make a new admin password's hash; paste it into Infisical as EASYDOCS_ADMIN_PASSWORD_HASH
	@cd backend && uv run python -m app.auth

uploads-to-r2:  ## move uploaded files still kept in Postgres into R2 (ARGS=--dry-run to just list them)
	infisical run --env=prod --path=/ --silent -- uv run --directory backend python scripts/uploads_to_r2.py $(ARGS)
