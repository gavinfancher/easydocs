# The VM that runs the site (Proxmox VM 506). Override with: make deploy HOST=ubuntu@<ip>
HOST ?= ubuntu@mydocs.ad.gavinf.com

.PHONY: dev test deploy logs set-password import

dev:     ## run locally with its own Postgres at http://localhost:8000 (no login)
	docker compose up --build

test:    ## needs Docker: starts a throwaway Postgres on 127.0.0.1:5433 the first time
	@docker start mydocs-test-pg >/dev/null 2>&1 || { docker run -d --name mydocs-test-pg -p 127.0.0.1:5433:5432 \
		-e POSTGRES_PASSWORD=test postgres:18.4 >/dev/null && sleep 4; }
	uv run pytest -q

deploy:  ## copy the app to the VM and restart it there
	rsync -az --delete --exclude .venv --exclude .git --exclude data --exclude deploy/.env \
		--exclude __pycache__ --exclude .pytest_cache --exclude .DS_Store ./ $(HOST):/opt/mydocs/
	ssh $(HOST) 'cd /opt/mydocs && docker compose -f deploy/compose.yml up -d --build --remove-orphans && docker image prune -f >/dev/null'

logs:    ## tail the app's logs on the VM
	ssh $(HOST) 'cd /opt/mydocs && docker compose -f deploy/compose.yml logs -f --tail 100'

set-password:  ## change the admin password on the VM (asks for it here; only the hash leaves this machine)
	@hash=$$(uv run python -m app.auth) && ssh $(HOST) "cd /opt/mydocs/deploy && \
		sed -i 's|^MYDOCS_ADMIN_PASSWORD_HASH=.*|MYDOCS_ADMIN_PASSWORD_HASH=$$hash|' .env && \
		docker compose up -d mydocs 2>/dev/null" && echo "Password changed. Sign in again at /admin/login."

import:  ## copy ./data from the old file-based version into a database: make import DB=postgresql://...
	MYDOCS_DATABASE_URL='$(DB)' uv run python scripts/import_data.py data
