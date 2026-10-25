# Production runs on the home k8s cluster (deploy/k8s, synced by Argo CD) behind Cloudflare (web/).
# Pushing to main deploys both; nothing here touches production except the vm-* targets.
# The old VM (Proxmox VM 506), until the cutover. Override with: make vm-deploy HOST=ubuntu@<ip>
HOST ?= ubuntu@easydocs.ad.gavinf.com

.PHONY: dev test logs password-hash uploads-to-r2 import vm-deploy vm-logs

dev:     ## run locally with its own Postgres at http://localhost:8000 (no login; uploads kept in Postgres)
	docker compose up --build

test:    ## needs Docker: starts a throwaway Postgres on 127.0.0.1:5433 the first time
	@docker start easydocs-test-pg >/dev/null 2>&1 || { docker run -d --name easydocs-test-pg -p 127.0.0.1:5433:5432 \
		-e POSTGRES_PASSWORD=test postgres:18.4 >/dev/null && sleep 4; }
	uv run pytest -q

logs:    ## tail the API's logs on the cluster
	kubectl -n easydocs logs deploy/easydocs -f --tail 100

password-hash:  ## make a new admin password's hash; paste it into Infisical as EASYDOCS_ADMIN_PASSWORD_HASH
	@uv run python -m app.auth

uploads-to-r2:  ## move uploaded files still kept in Postgres into R2 (needs the infisical CLI, signed in)
	infisical run --env=prod --path=/ -- uv run python scripts/uploads_to_r2.py

import:  ## copy ./data from the old file-based version into a database: make import DB=postgresql://...
	EASYDOCS_DATABASE_URL='$(DB)' uv run python scripts/import_data.py data

vm-deploy:  ## (old VM) copy the app to the VM and restart it there
	rsync -az --delete --exclude .venv --exclude .git --exclude data --exclude deploy/.env \
		--exclude __pycache__ --exclude .pytest_cache --exclude .DS_Store --exclude node_modules ./ $(HOST):/opt/easydocs/
	ssh $(HOST) 'cd /opt/easydocs && docker compose -f deploy/compose.yml up -d --build --remove-orphans && docker image prune -f >/dev/null'

vm-logs:    ## (old VM) tail the app's logs
	ssh $(HOST) 'cd /opt/easydocs && docker compose -f deploy/compose.yml logs -f --tail 100'
