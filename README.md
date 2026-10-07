<div align="center">

# 🚀 VPS Automation

**Automated Telegram bot hosting platform — deploy, manage, and scale bots without touching a server.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D?style=for-the-badge&logo=redis&logoColor=white)](https://redis.io)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docker.com)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)

</div>

---

## ✨ What is this?

**VPS Automation** is a fully-automated Telegram bot hosting platform. Users send a `/deploy` command, upload their bot as a ZIP, and the platform:

- 🔐 Automatically provisions a hosting account
- 📦 Uploads and validates their project files
- 🚀 Starts their bot on a cloud VPS
- 📊 Tracks status, logs, and uptime
- 🗄️ Auto-backs up their database before expiry
- 🧹 Cleans everything up after 24 hours

No dashboards, no signup forms, no configuration. Everything happens inside Telegram.

---

## 🎯 Features

<table>
<tr>
<td width="50%">

### 🤖 Telegram-First UX
- Full deployment lifecycle via chat
- Inline Confirm/Cancel buttons
- Live progress messages
- Owner-only admin commands

</td>
<td width="50%">

### 🚀 Automated Provisioning
- Real browser-driven signup
- OTP handling without user input
- Auto bot type detection
- Zero-touch cloud setup

</td>
</tr>
<tr>
<td>

### 🗄️ Smart Data Handling
- SQLite database detection
- Automatic export 30 min before expiry
- Manual `/download-db` on demand
- Original ZIP preserved

</td>
<td>

### 🔒 Security & Isolation
- Per-deployment browser profiles
- ZIP validation (path traversal, symlinks, encryption)
- Content screening for suspicious descriptions
- Server-side session auth (no JWT)

</td>
</tr>
</table>

---

## 🏗️ Architecture
┌─────────────────┐
│ Telegram │ User sends /deploy
│ User │
└────────┬────────┘
│
▼
┌─────────────────────────────────────────────┐
│ Controller (FastAPI) │
│ ┌──────────┐ ┌───────────┐ ┌──────────┐ │
│ │ aiogram │ │ Postgres │ │ Redis │ │
│ │ Bot │ │ (state) │ │ (queue) │ │
│ └────┬─────┘ └───────────┘ └──────────┘ │
│ │ │
│ ▼ │
│ ┌──────────────────────────────────────┐ │
│ │ Worker (async job processor) │ │
│ └──────────────┬───────────────────────┘ │
│ │ │
│ ▼ │
│ ┌──────────────────────────────────────┐ │
│ │ Selenium + Chrome (browser) │ │
│ └──────────────┬───────────────────────┘ │
└─────────────────┼───────────────────────────┘
│
▼
┌────────────────────┐
│ Hosting Provider │ Real VPS
│ (browser-driven) │
└────────────────────┘

text

---

## 📋 Commands

### 👤 User Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message + tariff info |
| `/help` | Full command reference |
| `/deploy` | Start a new deployment |
| `/cancel` | Abort an in-progress deploy |
| `/mydeployments` | List your last 10 deployments |
| `/status <ID>` | Full status of a deployment |
| `/logs <ID>` | Recent activity log |
| `/restart <ID>` | Restart your bot |
| `/stop <ID>` | Stop your bot |
| `/delete <ID>` | Permanently delete a deployment |
| `/download-db <ID>` | Download your database file |

### 👑 Owner Commands

| Command | Description |
|---------|-------------|
| `/adminlogin` | Get one-time admin panel login code |
| `/wipeall` | ⚠️ Wipe all deployments + storage |
| `/approve <ID>` | Approve a flagged deployment |
| `/reject <ID>` | Reject a flagged deployment |

---

## 🚀 Quick Start (Railway)

### 1. Prerequisites

- GitHub account
- Railway account
- Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Your Telegram numeric ID (from [@userinfobot](https://t.me/userinfobot))

### 2. Deploy

```bash
# Clone repo
git clone https://github.com/DEADxEVIL/VPS-AUTOMATION-AUTO-HOST.git
cd VPS-AUTOMATION-AUTO-HOST

# Install Railway CLI
npm i -g @railway/cli
railway login

# Create project
railway init

# Deploy
railway up
3. Add Databases
In Railway dashboard:

Click + New → Database → PostgreSQL

Click + New → Database → Redis

4. Set Environment Variables
In your app service → Variables:

env
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
TELEGRAM_BOT_TOKEN=your_bot_token_here
OWNER_TELEGRAM_ID=your_numeric_id
PUBLIC_BASE_URL=https://your-app.up.railway.app
STORAGE_ROOT=/app/storage
PELLA_HEADLESS=true
5. Attach Volume
Settings → Volumes → + New Volume → mount at /app/storage

6. Test
Send /start to your bot in Telegram.

🔧 Environment Variables
Variable	Required	Default	Description
DATABASE_URL	✅	—	PostgreSQL connection string
REDIS_URL	✅	—	Redis connection string
TELEGRAM_BOT_TOKEN	✅	—	Bot token from BotFather
OWNER_TELEGRAM_ID	✅	—	Numeric Telegram ID of owner
PUBLIC_BASE_URL	⬜	http://127.0.0.1:8000	Public URL for admin panel
STORAGE_ROOT	⬜	./storage	Path for ZIPs and profiles
CHROME_HEADLESS	⬜	false	Run Chrome headless
MAX_ZIP_BYTES	⬜	52428800	Max ZIP size (50 MB)
SERVER_READY_WAIT	⬜	25	Seconds to wait after create
DB_CHECK_WAIT	⬜	30	Seconds before DB file scan
📁 Project Structure
text
vps-automation/
├── main.py                  # Complete backend (single file)
├── requirements.txt         # Python dependencies
├── Dockerfile               # Container image
├── docker-compose.yml       # Local dev stack
├── .gitignore
├── .railwayignore
└── storage/                 # Runtime data (gitignored)
    ├── deployments/         # Original ZIPs + downloads
    └── pella_profiles/      # Chrome profiles per deployment
🔐 Security
No JWT — server-side sessions with secure HTTP-only cookies + CSRF

ZIP validation — protects against path traversal, symlinks, encrypted entries, zip bombs

Content screening — suspicious descriptions go to a review queue

Audit logging — every sensitive admin action is logged

Secret redaction — passwords, tokens, OTPs never appear in logs

Isolated sessions — each deployment gets its own browser profile

🗄️ Database Schema
Key tables (all managed automatically on startup):

Table	Purpose
users	Telegram user records
deployments	Deployment state + provider references
deployment_files	Original ZIP metadata
deployment_events	State machine transitions
deployment_logs	Per-deployment activity
admin_sessions	Server-side session storage
admin_audit_logs	Sensitive action trail
🛠️ Local Development
bash
# Clone
git clone https://github.com/DEADxEVIL/VPS-AUTOMATION-AUTO-HOST.git
cd VPS-AUTOMATION-AUTO-HOST

# Python env
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install
pip install -r requirements.txt

# Start dependencies (Docker)
docker compose up -d postgres redis

# Run app
uvicorn main:app --reload --host 0.0.0.0 --port 8000
📊 State Machine
Deployments flow through a strict state machine:

text
CREATED → VALIDATING → QUEUED → PROVISIONING → ACCOUNT_CREATING
       → HOST_CREATING → HOST_READY → STARTING → RUNNING
       → (24h TTL) → EXPIRING → DELETING → EXPIRED
Terminal states: EXPIRED, DELETED, VALIDATION_FAILED

🤝 Contributing
Fork the repo

Create a feature branch: git checkout -b feature/amazing

Commit: git commit -m "Add amazing feature"

Push: git push origin feature/amazing

Open a Pull Request

📄 License
MIT — see LICENSE

<div align="center">
Built with ❤️ using FastAPI, aiogram, and Selenium

⭐ Star this repo if it helps you!

</div> ```