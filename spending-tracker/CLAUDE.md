# You are in SpendTrack — NOT the fitness app

This directory is **one of two independent apps** in this repo. If you are
editing a file under `spending-tracker/`, you are working on SpendTrack.

**The other app (Path to Eldorado, the fitness tracker) lives at the repo
root.** Never change its files while working here. See the root `CLAUDE.md`
for the full two-app map.

## The trap: 15 files share the same relative path in both apps

`app.py` · `models.py` · `requirements.txt` · `.gitignore` ·
`templates/index.html` · `templates/login.html` ·
`static/style.css` · `static/sw.js` · `static/manifest.json` ·
`static/icon-180.png` · `static/icon-512.png` ·
`deploy/{setup-server,update-app,backup-db,add-ssl}.sh`

The fitness app is at the **repo root**, so a bare path like
`templates/index.html` or `app.py` **silently resolves to the fitness app**.
A repo-wide `grep "def login"` returns both `./app.py` and
`./spending-tracker/app.py` with nothing to distinguish them.

**Always use an explicit `spending-tracker/` prefix when you mean this app**,
and always check which path a search result came from before editing it.

## Confirm before you edit

```bash
bash tools/which-app.sh          # run from the repo root
```

It reports which app your current diff touches and refuses to guess when the
diff spans both.

## This app at a glance

| | |
|---|---|
| Code | `spending-tracker/` (this directory) |
| systemd service | `spendtrack` |
| gunicorn | `127.0.0.1:5001` |
| Database | `spending-tracker/instance/spending.db` |
| Env file | `spending-tracker/.env` (server only, never committed) |
| URL | https://financecop.duckdns.org |

The fitness app is `eldorado` / port 5000 / `instance/dashboard.db` /
https://pt-eldorado.duckdns.org — **touch none of those from here.**

## Imports resolve by working directory

`from models import ...` in this directory means
`spending-tracker/models.py`, **not** the fitness app's root `models.py`.
The two schemas are unrelated and share no code. Run this app with
`spending-tracker/` as the working directory, exactly as its systemd unit does.

## Before committing

A pre-commit hook blocks any commit touching both apps. If it fires, split
your work into two commits rather than bypassing it. Enable it in a fresh
clone with:

```bash
git config core.hooksPath .githooks
```

For everything else — billing cycles, the photo-import protocol, push
notifications, the Hebrew statement parser — see `spending-tracker/README.md`
and the root `CLAUDE.md`.
