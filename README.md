# QobuzProxy

A bridge between Qobuz Connect and DLNA speakers. Also supports local audio playback.

## Why?

Qobuz has a "Connect" feature (similar to Spotify Connect) that lets you control playback on supported devices from their app. Unfortunately, many popular speakers — most notably **Sonos** — don't support Qobuz Connect natively. This means you can't pick a Sonos speaker as a playback target in the Qobuz app, even though Sonos fully supports DLNA/UPnP streaming.

QobuzProxy solves this by acting as a virtual Qobuz Connect device on your network. When you open the Qobuz app, QobuzProxy shows up as a selectable speaker. When you play music, it receives the stream from Qobuz and forwards it to your DLNA-compatible speaker (like Sonos), preserving hi-res audio quality.

**In short:** Run QobuzProxy on a Raspberry Pi (or Docker or any always-on machine) on your local network, and your Sonos speakers become fully controllable Qobuz Connect targets — play, pause, skip, and adjust volume, all from the official Qobuz app.

<p align="center">
  <img src="docs/images/webui-speakers.png" alt="QobuzProxy Web UI" width="500">
</p>

## Features

- Appears as a Qobuz Connect device in the official Qobuz app
- Streams audio to DLNA renderers (Sonos, Denon HEOS, etc.)
- Local audio playback via PortAudio (play directly through your machine's speakers/DAC)
- **Web UI for speaker management** — discover, add, edit, and remove speakers from your browser
- Auto-detects device capabilities to select optimal audio quality
- Zero-config startup — boot with no config file, set everything up from the web UI
- Runs on Raspberry Pi, Docker, or any Linux/macOS system

## Audio Quality

By default (`max_quality: auto`), QobuzProxy queries your DLNA device's capabilities and automatically selects the best supported quality. You can also set a specific quality level:

| Value | Format |
|-------|--------|
| `auto` | Auto-detect from device (recommended) |
| `5` | MP3 320 kbps |
| `6` | FLAC CD (16-bit/44.1kHz) |
| `7` | FLAC Hi-Res (24-bit/96kHz) |
| `27` | FLAC Hi-Res (24-bit/192kHz) |

## Local Audio Playback

QobuzProxy can also play audio directly through your machine's speakers or DAC, without needing a DLNA device. Set the `QOBUZPROXY_BACKEND` environment variable to `local`:

```bash
docker run -d --network host \
  -v ./data:/data \
  -e QOBUZPROXY_BACKEND=local \
  --device /dev/snd \
  ghcr.io/leolobato/qobuz-proxy:latest
```

Note: The `--device /dev/snd` flag gives the container access to the host's audio devices (Linux only). Qobuz credentials should be in your `data/config.yaml`.

## Installation

A pre-built Docker image is available from GitHub Container Registry:

```bash
docker pull ghcr.io/leolobato/qobuz-proxy:latest
```

### Quick Start (Docker)

```bash
docker run -d --network host \
  -v ./data:/data \
  ghcr.io/leolobato/qobuz-proxy:latest
```

Then open **http://localhost:8689** in your browser, log in to Qobuz, and add your speakers from the web UI. No config file needed.

The `/data` volume persists auth tokens, credentials, and speaker configuration across restarts.

You can also pre-configure speakers with a `config.yaml` or environment variables — see [Configuration](#configuration) below.

View logs:
```bash
docker-compose logs -f
```

### Quick Start (without Docker)

```bash
pip install .
qobuz-proxy
```

Open **http://localhost:8689**, authenticate, and add speakers from the UI. Speaker configuration is saved to `config.yaml` in the current directory automatically.

To use a pre-existing config file: `qobuz-proxy --config /path/to/config.yaml`

### Raspberry Pi (ALSA + systemd)

This is the **single maintained Raspberry Pi installation procedure** for this
fork. It assumes Raspberry Pi OS, an existing checkout at
`~/src/qobuz-proxy`, an Audiolab USB DAC and the `uv` Python environment.
The [controller README](https://github.com/djcult/qobuz-pi-control/blob/dev/README.md#raspberry-pi-systemd-service)
contains the corresponding controller instructions. Other layouts are possible,
but require editing the unit paths.

#### 1. Prepare the proxy

If the proxy already plays music manually, **keep your existing `config.yaml`
and authentication cache**; you do not need to authenticate again.

```bash
sudo apt update
sudo apt install -y flac libasound2 alsa-utils
cd ~/src/qobuz-proxy
git switch alsa-dev
git pull --ff-only
uv sync --no-dev
test -f config.yaml
```

For a new checkout, install `uv` first, clone this repository into
`~/src/qobuz-proxy`, and create `config.yaml` from
[config.yaml.example](config.yaml.example); authenticate in the web UI.
Set `backend.type: alsa` and use an exact ALSA `hw:` device for bit-perfect
integer PCM (not `plughw:` or `default`). Identify the stable ALSA
card name with `cat /proc/asound/cards` and `aplay -L`; make sure your
service user can access `/dev/snd/*` (typically membership in `audio`,
requiring a logout/reboot to take effect). Existing users should preserve the
device configured for their working setup.

#### 2. Install the service

The tracked [`contrib/qobuz-proxy.service`](contrib/qobuz-proxy.service)
is a template, with `@USER@` and `@HOME@` placeholders. Install it with
your current login details:

```bash
cd ~/src/qobuz-proxy
sed -e "s|@USER@|$(id -un)|g" \
    -e "s|@HOME@|$HOME|g" \
    contrib/qobuz-proxy.service | \
    sudo tee /etc/systemd/system/qobuz-proxy.service >/dev/null

sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/qobuz-proxy.service
# Stop any manually running proxy first; ALSA hw devices are exclusive.
sudo systemctl enable --now qobuz-proxy.service
systemctl status qobuz-proxy.service
```

Press `q` to exit the status display. The unit runs directly from
`~/src/qobuz-proxy/.venv/bin/qobuz-proxy` with your existing
`~/src/qobuz-proxy/config.yaml`. It automatically restarts after an
unexpected exit and starts on boot.

#### 3. PCM spectrum environment variable

The **proxy** service contains:

```ini
Environment=QOBUZ_PCM_MONITOR_SOCKET=/tmp/qobuz-pcm.sock
```

That enables a separate, best-effort Unix-datagram PCM monitoring stream
used by `qobuz-pi-control` to draw the spectrum. It does **not** modify
the integer PCM sent to ALSA. The controller creates the socket, with mode
`0600`. Therefore run both services as the **same Linux user**, and
**do not set `PrivateTmp=true`**: doing so hides the socket from the
other process. The proxy continues to play if the controller is not running.

Remove that `Environment=` line and restart the proxy to disable the tap.

#### 4. Boot ordering, logs and updates

The controller unit starts after `qobuz-proxy.service` and separately waits
until `http://127.0.0.1:8689/api/speakers` responds. The `After=`
directive **does not itself guarantee API readiness**. A temporarily
unavailable controller does not stop proxy playback.

```bash
journalctl -u qobuz-proxy -f
systemctl show qobuz-proxy -p Environment
sudo systemctl restart qobuz-proxy
```

Stop any manual instance before starting the service. After updating the
checkout (`git pull --ff-only` and, if dependencies changed,
`uv sync --no-dev`), restart the service to run the new version. Keep
your existing configuration and authentication cache private. The
`main` branch is not required for this setup.

### Authentication

QobuzProxy authenticates via Qobuz's OAuth flow — just click a button and log in:

1. Start QobuzProxy (Docker or standalone).
2. Open **http://localhost:8689** in your browser.
3. Click **Log in to Qobuz** — you'll be redirected to the Qobuz sign-in page.
4. Log in with your Qobuz credentials.
5. You'll be redirected back to QobuzProxy, now authenticated.

The auth token is cached locally. You only need to do this once until the token expires. This works the same whether running locally, in Docker, or behind a reverse proxy.

**Power-user alternative:** You can skip the web UI by providing `auth_token` and `user_id` directly in your `config.yaml`:

```yaml
qobuz:
  user_id: "12345678"
  auth_token: "your-auth-token"
```

Or via environment variables: `QOBUZ_USER_ID` and `QOBUZ_AUTH_TOKEN`.

### Multi-Speaker Setup

A single QobuzProxy instance can manage multiple speakers. Each speaker appears as a separate device in the Qobuz app.

The easiest way to set up multiple speakers is through the web UI at **http://localhost:8689** — click **+ Add Speaker** for each device. The web UI will scan your network for DLNA devices and let you configure each one. Changes are saved to `config.yaml` automatically.

You can also configure speakers directly in `config.yaml`:

```yaml
speakers:
  - name: "Living Room"
    backend: dlna
    dlna_ip: "192.168.1.50"
    max_quality: auto

  - name: "Office"
    backend: dlna
    dlna_ip: "192.168.1.51"
    max_quality: 7

  - name: "Headphones"
    backend: local
    audio_device: "Built-in Output"
```

Ports are auto-assigned unless explicitly set via `http_port` and `proxy_port`. See `config.yaml.example` for all available options.

### Network Requirements

**Important**: QobuzProxy requires `network_mode: host` (Docker) or direct host access for mDNS discovery to work. This allows the Qobuz app to find the device on your local network.

If you cannot use host networking, consider:
- Using a macvlan network with a dedicated IP on your LAN
- Running QobuzProxy directly on the host (not in Docker)

QobuzProxy registers discovery on the interface whose IPv4 address it advertises.
It does not join multicast groups on every Docker bridge, so hosts with many
containers do not need a higher multicast membership limit for Qobuz discovery.

### Configuration

The config file is found automatically in this order:

1. `--config` CLI argument (if provided)
2. `./config.yaml` (current directory)
3. `$QOBUZPROXY_DATA_DIR/config.yaml` (set to `/data` in the Docker image)

Environment variables and CLI arguments override config file values. See `.env.example` for available environment variables.

### Data Directory

In Docker, both the config file and credential cache live under `/data`:

```yaml
volumes:
  - ./data:/data
```

This directory stores auth tokens and the Qobuz web player credential cache so they persist across restarts. Outside Docker, the cache defaults to `~/.qobuz-proxy/`.

### Health Check

The container includes a health check that verifies the HTTP server is responding:
```bash
docker inspect --format='{{.State.Health.Status}}' qobuz-proxy
```

## Contributing

Bug reports and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first: keep each PR to a single change, leave version bumps to the maintainer, and say how you tested on real hardware.

## Acknowledgments

This project is based on the Qobuz Connect reverse-engineering work done by [Tobias Guyer](https://github.com/tobiasguyer) in [StreamCore32](https://github.com/tobiasguyer/StreamCore32). Thanks to his efforts in figuring out the Qobuz Connect protocol, this project was possible.

## Disclaimer

This project was built almost entirely through agentic programming using [Claude Code](https://claude.ai/claude-code). The architecture, implementation, and tests were generated through AI-assisted development with human guidance and review.

## License

[MIT](LICENSE)
