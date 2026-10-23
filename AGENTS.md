# Notes for AI assistants

- **Deploying this repo to a server**: follow [DEPLOY.md](DEPLOY.md) step by step. The whole install is
  `sudo DOMAIN=<domain> ./deploy/install.sh`; ask the user only for the domain (and optionally an email and the
  admin account name). Let the user type the admin password in the terminal, or let the script generate one.
- **Never** print, store or ask the user to paste a WeCom cookie. The server must keep working with each caller's
  own cookie only (`--caller-cookies-only`).
- **Installing the client for a user**: open `https://<their server>/llms.txt` and follow it.
- Development: `uv sync && uv run pytest`. Details in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
- Files under `deploy/` use the placeholder domain `wecom.example.com`; keep it that way in the repo.
- Changing `deploy/site/wecom-mcp.py`: bump its `VERSION` (installed clients update themselves by comparing it).
  Keep the commands in `deploy/site/index.html` and `llms.txt` the same.
- Test fixtures must not contain real sheet data: use `scripts/anonymize_fixture.py`.
