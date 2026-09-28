<div align="center">

# 🎯 OrbCompletorByAzmal

**Auto-complete Discord orb quests on linked accounts — one command, zero waiting.**

A single-file Discord bot. No modules, no config sprawl, no fuss.

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![discord.py](https://img.shields.io/badge/discord.py-2.3%2B-5865F2?logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Made by Azmal](https://img.shields.io/badge/made%20by-Azmal-9b59b6.svg)](#-credits)

[Features](#-features) •
[Quick Start](#-quick-start) •
[Commands](#-commands) •
[How It Works](#-how-it-works) •
[VPS Setup](#-running-24-7-on-a-vps) •
[FAQ](#-faq) •
[Troubleshooting](#-troubleshooting)

</div>

---

## ✨ Features

| | |
|---|---|
| 🎯 **Orb quest automation** | Detects orb reward quests and completes them end-to-end |
| 🔗 **Per-user linking** | Each user links their own account via DM — no shared tokens |
| 🧠 **Smart detection** | Scans rewards + reward config to correctly identify orb quests |
| ⏱️ **Rate-limit aware** | Reads `retry_after` from Discord on 429 and backs off |
| 🔁 **Auto-retry** | Failed quests get a second attempt before giving up |
| 📊 **Run history** | Every run logged — `*stats` shows your completions |
| 👑 **Owner panel** | Stats, cooldown control, user management for the bot owner |
| ⚡ **No cooldown for owner** | Owner can spam `*questall` without limits |
| 🗃️ **Single file** | Everything in `bot.py` — trivial to audit, fork, and modify |
| 🚀 **One-command run** | `python3 bot.py` and you're live |

---

## 📦 Quick Start

```bash
# 1. Clone
git clone https://github.com/baconzoro87-svg/OrbCompletorByAzmal.git
cd OrbCompletorByAzmal

# 2. Environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Dependencies
pip install -r requirements.txt

# 4. Config — fill BOT_TOKEN and OWNER_ID
cp .env.example .env
nano .env

# 5. Run
python3 bot.py
