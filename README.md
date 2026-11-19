# Private Cloud Blueprint

A working home server for people who want their messages, home and files on a box they own.
Published by Sterling Management.

## What it does
- **One private inbox.** A Matrix server (Tuwunel) with bridges for Telegram, Signal and iMessage. It talks to no other Matrix servers and is reachable only over Tailscale.
- **Chat with your house.** Home Assistant takes plain-language commands from one chat room, on an allowlist of devices, with a daily cap on bot messages and quiet hours.
- **An AI assistant that can read but not send.** A read-only gateway lets a local model (no cloud) read chosen chats. It can only write drafts; a draft is sent when the owner reacts with a check mark.
- **A hard line between work and personal.** Work chats are dropped at the bridge and never reach the server.
- **Backups you can restore.** Nightly encrypted backups (restic) to off-site storage, with a heartbeat that alerts when a backup fails or stops running.
- **Monitoring.** Uptime Kuma, ntfy push alerts, image-update notices (Diun) and a network watchdog.

## Layout
| Path | What |
|---|---|
| `stacks/matrix/` | Tuwunel and the mautrix bridges, including a small patch that makes the iMessage bridge ignore listed chats |
| `stacks/homeassistant/` | Compose file and YAML: chat commands through Assist, capped alerts |
| `stacks/goose/` | The read-only gateway for the AI assistant |
| `stacks/monitoring/` | Uptime Kuma, ntfy, Diun, network watchdog |
| `scripts/` | Backup, heartbeat, nightly housekeeping, Home Assistant updates, crontab |
| `host/` | systemd units and host settings |

## Before you use it
- Every secret is a placeholder: `<SECRET name>` or `<REDACTED>`. Put your own values in files outside Git.
- Names, domains and addresses are examples: `home.example.ts.net`, `100.64.0.10` (server), `100.64.0.20` (the Mac that relays iMessage), user `homelab`, chat user `@owner`.
- This is a snapshot of one real setup, not an installer. Read each file before you run it.
- The bridge configs are the upstream defaults with a few settings changed.

## How this copy is made
A script copies the private configuration, replaces anything that identifies the owner, then refuses to finish if a name, address, number or any real secret value is still present.

## Licence
MIT. See `LICENSE`. Bridge configuration files derive from the mautrix projects and keep their own licences.

## Support
If this saved you time, you can support the work: *(link to come)*.
