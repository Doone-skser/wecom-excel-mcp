# Notes for AI assistants

- **Deploying this repo to a server**: follow [DEPLOY.md](DEPLOY.md). On Linux the whole install is
  `curl -fsSL https://raw.githubusercontent.com/Doone-skser/wecom-excel-mcp/main/deploy/install.sh | sudo bash`
  (prebuilt executable, port 8765, nothing compiled); add `DOMAIN=<domain>` only when the user wants HTTPS.
  Elsewhere: download the release executable and run `wecom-excel-mcp serve`.
- **Never** print, store or ask the user to paste a WeCom cookie. The server must keep working with each caller's
  own cookie only (`--caller-cookies-only`).
- **Installing the client for a user**: open `https://<their server>/llms.txt` and follow it.
- Development: `uv sync && uv run pytest`. Details in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
- The setup page, `llms.txt` and the client live in `src/wecom_excel_mcp/site/` and say `https://wecom.example.com`;
  the server replaces that with its own address when serving them. Keep the placeholder in the repo.
- Changing `site/wecom-mcp.py`: bump its `VERSION` (installed clients update themselves by comparing it).
  Keep the commands in `site/index.html` and `llms.txt` the same.
- Releases: bump `version` in `pyproject.toml` and `rust/Cargo.toml`, push a tag `vX.Y.Z`; `.github/workflows/release.yml`
  builds the executables (smoke-tested with `packaging/smoke_test.py`), wheels and sdist, and publishes the release.
- Test fixtures must not contain real sheet data: use `scripts/anonymize_fixture.py`.
