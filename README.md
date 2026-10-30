# continuous-working-agent

A self-hosted AI worker that runs 24/7. It works through a task queue and narrates every
step live. You can control it three ways:

| Interface | How |
|---|---|
| **Web dashboard** | live feed, approval buttons, queue, goal, recent tasks with step details, pause / stop / verbosity |
| **CLI** | `python main.py --cli` runs the agent in your terminal, and `python cli.py` attaches to the running service |
| **Telegram** (optional) | the same commands from your phone, with live step messages |

All three talk to one agent at the same time. A task you add on the web shows up live in
the CLI and in Telegram. You can answer an approval from whichever interface you
see it in first.

It works with any OpenAI-compatible chat-completions API that supports tool calling:
OpenRouter, Nous Portal, OpenAI, or Anthropic's OpenAI-compatible endpoint.

```
11:44:41  ▶️ Task #1: list the files in WORKDIR and tell me how many there are
11:44:41  🧠 thinking: I'll list the working directory.
11:44:41  📂 listing: .
11:44:41  ✅ ok — 3 files, 0 dirs
11:44:41  🏁 Task #1 done · 1 steps · 1.1k tokens
          There are 3 files in WORKDIR.
```

---

## Run it on Kali Linux (or any Debian/Ubuntu)

### 1. Install the packages it needs
```bash
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip
```

### 2. Download the code and install the Python packages
```bash
git clone https://github.com/HIKMAT-NOUN/continuous-working-agent ~/myagent
```
```bash
cd ~/myagent && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### 3. Configure
```bash
cd ~/myagent && cp .env.example .env && chmod 600 .env && nano .env
```
Set at least `API_BASE_URL`, `API_KEY`, `MODEL` and `WORKDIR`. Telegram is optional. To use
it, fill in `TELEGRAM_BOT_TOKEN` (from @BotFather, `/newbot`) and `ALLOWED_USER_ID` (your
numeric id from @userinfobot), and press **Start** in a chat with your bot once.

### 4a. Run it in your terminal (CLI)
```bash
cd ~/myagent && .venv/bin/python main.py --cli
```
This runs the agent in your terminal and prints the web dashboard link with its login
token. Type a task in plain text, or a `/command`. When an approval comes up, type `y` or
`n`. `/quit` or Ctrl+D stops the agent. To run with only the terminal, add
`--no-web --no-telegram`.

### 4b. Or run it 24/7 as a service (recommended)
```bash
cd ~/myagent && ./deploy/install.sh
```
Then use it from:
- **The terminal.** This attaches to the running service. Leaving the console does not
  stop the agent.
  ```bash
  cd ~/myagent && .venv/bin/python cli.py
  ```
- **The browser.** This prints the login link:
  ```bash
  cd ~/myagent && .venv/bin/python cli.py url
  ```
  Open the printed `http://127.0.0.1:8765/?token=…` in Firefox. The token is stored in a
  cookie, so you only need the link once.
- **One-off commands from scripts:**
  ```bash
  cd ~/myagent && .venv/bin/python cli.py send "/status"
  ```
- **Telegram**, if you configured it.

Running `main.py --cli` while the service is up attaches to the service instead of
starting a second agent.

Service management:
```bash
sudo systemctl status myagent
```
```bash
sudo systemctl restart myagent
```
```bash
journalctl -u myagent -f
```

### Opening the dashboard from another computer
The dashboard listens only on `127.0.0.1`, because anyone who can log in can run commands
through the agent. To reach it from another machine, use an SSH tunnel. Run this on your
laptop:
```bash
ssh -L 8765:127.0.0.1:8765 kali@KALI_IP
```
Then open `http://127.0.0.1:8765/?token=…` on your laptop. If you do set
`WEB_HOST=0.0.0.0`, put it behind HTTPS (for example a reverse proxy) and keep the token
secret.

### Updating
```bash
cd ~/myagent && git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart myagent
```

---

## Commands (the same in web, CLI and Telegram)
Plain text without a leading `/` is added as a task.

| Command | |
|---|---|
| `/task <text>` | add a task |
| `/goal <text>` | set the standing goal (`/goal clear` removes it, `/goal` shows it) |
| `/status` | state, current task, step and last action, queue, goal, today's tokens and cost |
| `/queue` | list pending tasks |
| `/pause`, `/resume` | pause or resume the worker (`/resume force` ignores the daily budget) |
| `/stop` | abort the current task, including a running shell command |
| `/approve [id]`, `/deny [id]` | answer a pending approval (`y`/`n` in the CLI) |
| `/verbose off\|steps\|full` | narration level for Telegram and the CLI |
| `/log` | last 20 steps |

## How it behaves
- **Task queue.** A background worker takes the next pending task and runs an agent loop
  (LLM → tool calls → results → LLM …) until the task is done, fails, or reaches
  `MAX_STEPS_PER_TASK`. One step is one tool call.
- **Standing goal.** When the queue is empty, the worker runs a goal cycle every
  `GOAL_INTERVAL` minutes, and each cycle sees the reports from earlier cycles. When the
  model reports `GOAL COMPLETE`, the goal is cleared.
- **Live narration.** You get a message before every tool call (`⚙️ running`,
  `🌐 fetching`, `📄 writing`, `📖 reading`, `📂 listing`, `🧠 thinking`). After each call
  you get the result (`✅ ok` / `❌ error` / `🚫 denied`), and every task ends with a final
  summary. Every event is stored in SQLite, so the web feed shows history after a
  restart.
- **Verbosity.** Telegram and the CLI follow `/verbose`: `off` shows only finals, approvals
  and alerts, `steps` is the default, and `full` adds up to 500 chars of tool output. The
  web feed always receives everything, and you can hide tool output with a checkbox.
- **Telegram rate limits.** Messages go out at most 1 per second. A backlog is merged into
  one message, never dropped. Errors are retried with backoff and never crash the agent.
- **Approvals.** Risky shell commands are paused until you approve them from any
  interface. If nobody answers within `APPROVAL_TIMEOUT`, the command is denied.
- **Persistence.** Tasks, each task's conversation (saved after every step), the step
  log, events, settings and usage all live in SQLite (`data/agent.db`). A task that was
  interrupted by a restart resumes from its last step. A lock file stops two agents from
  using the same data at once.
- **Budget.** Usage is counted in tokens per UTC day. When `DAILY_TOKEN_BUDGET` is
  reached, the worker pauses, tells you, and resumes after midnight UTC.
- **Secrets.** The API key, bot token and web token are scrubbed from every message, event
  and log line. They're also removed from the environment of shell commands.

## Agent tools
| Tool | Notes |
|---|---|
| `shell(command, timeout)` | bash in `WORKDIR`, default 120 s, max 1800 s; killed on timeout or `/stop` |
| `read_file`, `write_file`, `list_dir` | restricted to `WORKDIR`; symlinks that escape it are refused |
| `web_fetch(url)` | http(s) GET; HTML is converted to text, then truncated |
| `send_update(text)` | a free-form progress note to you |

These shell commands need approval: recursive `rm`, `sudo`/`su`/`doas`, shutdown/reboot,
`curl|wget … | sh`, `dd`, `mkfs`/disk tools, `chmod 777`, `chown`,
`systemctl`/`service`/`crontab`, `killall`/`pkill`, `eval`, fork bombs, raw device
writes, `$HOME`, and any path that resolves outside `WORKDIR`. Unparseable commands need
approval too.

> ⚠️ This check is a guardrail, not a sandbox. Run the agent as a normal user (on Kali,
> `kali` rather than root) and keep anything sensitive out of that user's reach.

## Tests
```bash
cd ~/myagent && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q
```

## Layout
```
main.py         entry point: wires worker + interfaces, --cli/--no-web/--no-telegram, lock
cli.py          CLI client: attach / run / send / url / token
console.py      terminal rendering + stdin loop (used by main.py --cli and cli.py)
web.py          aiohttp server: dashboard, WebSocket live feed, JSON API, token auth
static/         the dashboard (single HTML file, no build step)
telegram_ui.py  Telegram sink + handlers (only ALLOWED_USER_ID)
notifier.py     rate-limited, merging, retrying Telegram sender
commands.py     command processor shared by all interfaces
hub.py          event hub: agent → every interface, redaction, event log
worker.py       forever loop: queue, goal cycles, pause/budget, /stop, status
agent.py        LLM ↔ tools loop and narration
tools.py        shell, read_file, write_file, list_dir, web_fetch, send_update
safety.py       WORKDIR sandboxing + dangerous-command detection
approvals.py    approval requests answered from any interface
llm.py          OpenAI-compatible client with retries
db.py           SQLite: tasks, steps, events, settings, usage
config.py       .env loading + secret redaction
deploy/         systemd unit + install script
```
