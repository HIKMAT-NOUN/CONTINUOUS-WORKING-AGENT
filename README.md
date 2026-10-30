# myagent — a Telegram-controlled, always-on AI worker

A self-hosted agent that works through a task queue 24/7 and narrates every step
to your Telegram chat as it happens. It works with any OpenAI-compatible
chat-completions API that supports tool calling (OpenRouter, Nous Portal, OpenAI,
Anthropic's OpenAI-compatible endpoint, …).

```
📥 Task #1 queued.
▶️ Task #1: list the files in WORKDIR and tell me how many there are
🧠 thinking: I'll list the working directory.
📂 listing: .
✅ ok — 3 files, 0 dirs
🏁 Task #1 done · 1 steps · 1.1k tokens

There are 3 files in WORKDIR: notes.md, todo.txt, app.py.
```

## Features
- **Task queue.** Add work with `/task`. A background worker picks up the next pending
  task and runs an agent loop (LLM → tool calls → results → LLM …) until the task is
  done, fails, or reaches `MAX_STEPS_PER_TASK`.
- **Standing goal.** Set one with `/goal`. When the queue is empty, the worker runs a
  "goal cycle" every `GOAL_INTERVAL` minutes. Each cycle sees the reports from earlier
  cycles. When the model reports `GOAL COMPLETE`, the goal is cleared.
- **Live narration.** You get a message before every tool call (`⚙️ running`,
  `🌐 fetching`, `📄 writing`, `📖 reading`, `📂 listing`, `🧠 thinking`). After each call
  you get a one-line result (`✅ ok …` / `❌ error …` / `🚫 denied`), and every task
  ends with a final summary. Messages go out even when you haven't written to the bot.
- **Verbosity.** `/verbose off|steps|full`. `off` sends only final summaries, approvals
  and alerts. `steps` is the default. `full` also includes tool output (max 500 chars).
- **Rate-limit safe.** All outgoing messages go through a single queue that sends at
  most 1 message per second. When messages back up, they're merged into one (up to
  Telegram's 4096-char limit) rather than dropped. Flood-control (`RetryAfter`) and
  network errors are retried with backoff. A send error never crashes the agent.
- **Approvals.** Risky shell commands are held until you answer with inline
  **Approve / Deny** buttons. If you don't answer within `APPROVAL_TIMEOUT`, the
  command is denied.
- **Persistent.** Tasks, the conversation for each task (saved after every step), the
  step log, settings (goal, pause, verbosity) and token usage all live in SQLite. If
  the process restarts mid-task, the task picks up where it left off.
- **Budget.** Usage is counted in tokens per UTC day. When `DAILY_TOKEN_BUDGET` is
  reached, the worker pauses (the current task is saved), tells you, and resumes
  automatically after midnight UTC. `/resume force` overrides the budget for the rest
  of the day.
- **No secret leaks.** The API key and bot token are scrubbed from every Telegram
  message and every log line. They're also removed from the environment of shell
  commands, and httpx request logging is silenced because it would print the bot
  token as part of the URL.

## Commands
| Command | |
|---|---|
| `/task <text>` | add a task |
| `/goal <text>` | set the standing goal (`/goal clear` removes it, `/goal` shows it) |
| `/status` | state, current task + step + last action, queue size, goal, today's tokens and cost |
| `/queue` | list pending tasks |
| `/pause` / `/resume` | pause or resume the worker (takes effect before the next LLM call; `/resume force` ignores the budget) |
| `/stop` | abort the current task (also kills a running shell command) |
| `/verbose off\|steps\|full` | narration level |
| `/log` | last 20 steps |

## Agent tools
| Tool | Notes |
|---|---|
| `shell(command, timeout)` | bash in `WORKDIR`, default 120 s, max 1800 s, output is truncated for the model. Runs in its own process group, which is killed on timeout or `/stop`. |
| `read_file`, `write_file`, `list_dir` | restricted to `WORKDIR`; symlinks that point outside it are refused |
| `web_fetch(url)` | http(s) GET; HTML is converted to text, then truncated |
| `send_update(text)` | free-form progress note to you (`💬 …`) |

These shell commands need your approval: recursive `rm` (`rm -rf`, `rm -r`, …),
`sudo`/`su`/`doas`, shutdown/reboot/halt, `curl|wget … | sh`, `dd`, `mkfs`/disk tools,
`chmod 777`/`a+rwx`, `chown`, `systemctl`/`service`/`crontab`, `killall`/`pkill`,
`eval`, fork bombs, raw device writes, `cd` to home, `$HOME`, and any absolute, `~` or
`..` path that resolves outside `WORKDIR` (`/dev/null` and similar are allowed).
Unparseable commands also need approval.

> ⚠️ This check is a guardrail, not a sandbox. A model that is determined to get around
> it could obfuscate a command. Run the service as an unprivileged user with no
> passwordless sudo, and keep anything sensitive out of that user's reach. A dedicated
> user or a VM/container is best.

## Setup (Linux with systemd, Python 3.11+; tested target: Kali / Debian)

On Kali/Debian first: `sudo apt update && sudo apt install -y git python3 python3-venv python3-pip`

1. **Create a bot.** In Telegram, message [@BotFather](https://t.me/BotFather), send
   `/newbot`, and copy the token. Then message [@userinfobot](https://t.me/userinfobot)
   to get your numeric user id.
2. **Open a chat with your bot and press Start.** Telegram doesn't let a bot message you
   first, so it can't narrate until you've done this once.
3. **Install the code** to `~/myagent`:
   ```bash
   git clone https://github.com/HIKMAT-NOUN/continuous-working-agent ~/myagent
   ```
   ```bash
   cd ~/myagent && cp .env.example .env && chmod 600 .env
   ```
   Edit `.env` and set `API_BASE_URL`, `API_KEY`, `MODEL`, `TELEGRAM_BOT_TOKEN`,
   `ALLOWED_USER_ID` and `WORKDIR`. `WORKDIR` is created if it doesn't exist.
4. **(Optional) Try it in the foreground first:**
   ```bash
   cd ~/myagent && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
   ```bash
   cd ~/myagent && .venv/bin/python main.py
   ```
   You should get `🟢 Agent online` in Telegram. Press Ctrl+C to stop.
5. **Install and start the systemd service.** The script creates the venv if it's
   missing, writes `/etc/systemd/system/myagent.service` with your user and path, and
   runs `enable --now`:
   ```bash
   cd ~/myagent && ./deploy/install.sh
   ```
   To do the same by hand:
   ```bash
   sed -e "s#__USER__#$(id -un)#g" -e "s#__DIR__#$HOME/myagent#g" ~/myagent/deploy/myagent.service | sudo tee /etc/systemd/system/myagent.service
   ```
   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now myagent
   ```
6. **Day to day:**
   ```bash
   sudo systemctl status myagent
   ```
   ```bash
   journalctl -u myagent -f
   ```
   ```bash
   tail -f ~/myagent/logs/agent.log
   ```
   ```bash
   sudo systemctl restart myagent
   ```
   `Restart=always` brings it back after a crash, and `enable` starts it on boot. Restart
   the service after you edit `.env`.

## Quick test
Send this in Telegram:
```
/task list the files in WORKDIR and tell me how many there are
```
You should see `📥 Task #1 queued`, `▶️ Task #1: …`, `🧠 thinking: …`,
`📂 listing: .` (or `⚙️ running: ls …`), `✅ ok — N files, M dirs`, and finally
`🏁 Task #1 done …` followed by the count. Then try `/status`, `/log`, and a task that
triggers an approval, for example `/task delete the folder tmp with rm -rf`.

## Tests
No Telegram account or API key needed:
```bash
cd ~/myagent && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q
```
The tests cover the dangerous-command and path checks; notifier merging, splitting,
retry and redaction; approvals; and full worker runs against a scripted LLM. The worker
runs include the quick-test scenario, denied commands, the step limit, `/stop` killing a
running command, the budget pause, and resuming after a restart.

## Layout
```
main.py       entry point: logging (with redaction), Telegram polling, worker, shutdown
bot.py        command + button handlers (only ALLOWED_USER_ID)
worker.py     forever loop: queue → agent, goal cycles, pause/budget, /stop, /status
agent.py      the LLM ↔ tools loop and narration
tools.py      shell, read_file, write_file, list_dir, web_fetch, send_update
safety.py     WORKDIR path sandboxing + dangerous-command detection
approvals.py  Approve/Deny inline-button flow
notifier.py   rate-limited, merging, retrying Telegram sender
llm.py        OpenAI-compatible chat-completions client with retries
db.py         SQLite: tasks, steps, settings, usage
config.py     .env loading + secret redaction
deploy/       systemd unit + install script
```

## Notes
- **Step counting.** One step is one tool call. `MAX_STEPS_PER_TASK` caps the tool
  calls in a task. When the cap is hit, the model is asked once more, with no tools,
  for a summary, and the task is marked `failed` (step limit).
- **Cost.** The `/status` cost uses the provider's reported cost when it includes one
  (OpenRouter does). Otherwise it's estimated from `PRICE_INPUT_PER_M` /
  `PRICE_OUTPUT_PER_M`.
- **Anthropic endpoint.** Use `API_BASE_URL=https://api.anthropic.com/v1/` with your
  Anthropic key as `API_KEY`.
- **Restarts.** A task that was interrupted by a restart resumes from its last
  completed step. If a shell command was running at that moment, it's killed and the
  model is told to check the state before continuing.
