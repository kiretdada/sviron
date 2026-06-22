# Chromium Clone Remote Access System

This project creates 10 independent Chromium instances. Each clone has its own folder, `.env`, browser profile, cookies, session storage, cache, downloads, and launch configuration.

Each clone exposes the live active tab through a normal browser URL:

```text
http://SERVER_IP:PORT/PATH
```

The browser window runs on the server in headed Chromium. Remote users interact with the same live tab through a lightweight web gateway bound to `0.0.0.0`.

## Install

```powershell
python -m pip install -r requirements.txt
python .\tools\setup.py
```

`tools\setup.py` downloads Chromium through Playwright and creates:

```text
clones/
  chromium-01/
    .env
    launch.cmd
    launch.ps1
    profile/
    cache/
    downloads/
    config/
    logs/
  ...
  chromium-10/
```

## Start All 10 Clones

```powershell
python .\tools\run_all.py
```

## Start One Clone

```powershell
python .\tools\run_clone.py --clone .\clones\chromium-01
```

You can also run the per-clone launcher:

```powershell
.\clones\chromium-01\launch.ps1
```

## URLs

Each clone has a unique `.env`:

```env
PORT=1000
PATH=/abcd1234
```

Open the matching remote URL:

```text
http://SERVER_IP:1000/abcd1234
```

Use the actual `PORT` and `PATH` from that clone's `.env`.

## Security

Anyone who can reach a clone URL can view and control that Chromium tab, including logged-in sessions. Use firewall rules, private networking, VPN access, or a reverse proxy with authentication before exposing these ports beyond trusted users.

## Notes

- The Chromium binary is downloaded once by Playwright.
- Each clone is isolated by its own persistent `profile` directory and disk cache directory.
- The remote page streams and controls the active tab content. Browser chrome such as the address bar is not rendered in the remote page, but the headed Chromium window is still running on the server.
