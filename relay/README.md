# myagent relay

A tiny public meeting point so your phone app can reach the agent **without opening any
port** on the machine the agent runs on. Both the agent and the app dial *out* to this
relay; it forwards events one way and commands the other.

```
phone app  ──wss──►  RELAY (this)  ◄──wss──  agent (on your Kali box)
```

It keeps one agent connection and any number of app connections, buffers the last ~500
events for catch-up, and stores nothing on disk.

## Keys
Pick two long random secrets (e.g. `openssl rand -hex 24` each):
- `RELAY_AGENT_KEY` — the agent proves itself with this.
- `RELAY_USER_TOKEN` — the phone app proves itself with this.

Keep them secret: anyone with `RELAY_USER_TOKEN` can send commands to your agent.

## One-shot setup on a VM (Oracle, or any Linux) + Cloudflare Tunnel
If you have a VM you can SSH into and a domain on Cloudflare, this script does the whole
setup — installs the relay and cloudflared, runs both as services, and prints the exact
values for the app and the agent's .env. It opens no ports (the tunnel dials out). Re-run
safe. The Cloudflare login (browser) and your domain are the only manual bits.
```bash
curl -fsSL https://raw.githubusercontent.com/HIKMAT-NOUN/continuous-working-agent/main/relay/setup-oracle.sh -o setup-oracle.sh
bash setup-oracle.sh agent.yourdomain.com
```

## Run it (pick one host)

**Docker (any VPS):**
```bash
docker build -t myagent-relay .
docker run -d --restart always -p 8900:8900 \
  -e RELAY_AGENT_KEY=... -e RELAY_USER_TOKEN=... --name relay myagent-relay
```

**systemd on a VPS:** copy `relay.py` to `/opt/myagent-relay/`, edit the keys in
`myagent-relay.service`, then:
```bash
sudo cp myagent-relay.service /etc/systemd/system/ && sudo systemctl enable --now myagent-relay
```

**Fly.io (free-ish, gives HTTPS for free):**
```bash
fly launch --no-deploy
fly secrets set RELAY_AGENT_KEY=... RELAY_USER_TOKEN=...
fly deploy
```

## TLS (important)
Tokens and traffic must be encrypted. Fly.io terminates HTTPS for you. On a bare VPS, put
the relay behind a reverse proxy with a certificate (Caddy is one line) so clients use
`wss://` and `https://`:
```
your-relay.example {
  reverse_proxy 127.0.0.1:8900
}
```
Then your URLs are:
- agent:  `wss://your-relay.example/ws/agent`
- app:    `wss://your-relay.example/ws/app`

## Point the agent at it
In the agent's `.env`:
```
RELAY_URL=wss://your-relay.example/ws/agent
RELAY_AGENT_KEY=...     # same as the relay's RELAY_AGENT_KEY
```
Restart the agent. `GET /healthz` on the relay should then show `"agent_online": true`.

## Protocol (for the app)
Connect to `wss://your-relay.example/ws/app?after=0` with header
`Authorization: Bearer <RELAY_USER_TOKEN>` (or `?token=<RELAY_USER_TOKEN>`).
- On connect the relay sends `{"type":"hello","agent_online":bool,"events":[...],"state":{...}}`.
- Live messages: `{"type":"event","event":{…}}`, `{"type":"state","state":{…}}`,
  `{"type":"reply","ref":…,"html":…}`, `{"type":"presence","agent_online":bool}`.
- Send a command: `{"type":"command","text":"list the files","ref":"abc"}`.

Each `event` is the same shape the dashboard uses: `{id, ts, kind, level, html, data, agent}`.
`state` is the full status snapshot (current task, fleet/agents, queue, tokens, …).
