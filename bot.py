"""
Best Quest Completer — made by Azmal
Plaintext storage build.

Commands: *help *ping *about *link *unlink *status *quests *questall *stats
Owner:    *owner *users *setcd *say *unlinkuser
"""

import os
import time
import json
import asyncio
import sqlite3

import discord
from discord.ext import commands
from dotenv import load_dotenv
import requests

# ------------------------------------------------------------------ branding
BOT_NAME = "Best Quest Completer"
AUTHOR = "Azmal"
VERSION = "4.2-plain"
FOOTER = f"{BOT_NAME} — by {AUTHOR}"

# ------------------------------------------------------------------ config
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
DB_PATH = os.environ.get("DB_PATH", "tokens.db")
PREFIX = "*"
DEFAULT_COOLDOWN = 120

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing from .env")


# ------------------------------------------------------------------ database
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS tokens (
        user_id   INTEGER PRIMARY KEY,
        token     TEXT NOT NULL,
        linked_at INTEGER NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS runs (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        quest   TEXT NOT NULL,
        status  TEXT NOT NULL,
        ts      INTEGER NOT NULL
    )""")
    return c


def save_token(user_id, token):
    with _conn() as c:
        c.execute(
            "INSERT INTO tokens (user_id, token, linked_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET token=excluded.token, linked_at=excluded.linked_at",
            (user_id, token, int(time.time())),
        )


def get_token(user_id):
    with _conn() as c:
        row = c.execute("SELECT token FROM tokens WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else None


def delete_token(user_id):
    with _conn() as c:
        cur = c.execute("DELETE FROM tokens WHERE user_id=?", (user_id,))
        return cur.rowcount > 0


def log_run(user_id, quest, status):
    with _conn() as c:
        c.execute("INSERT INTO runs (user_id, quest, status, ts) VALUES (?, ?, ?, ?)",
                  (user_id, quest, status, int(time.time())))


def get_stats(user_id, limit=10):
    with _conn() as c:
        rows = c.execute(
            "SELECT quest, status FROM runs WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        done = c.execute("SELECT COUNT(*) FROM runs WHERE user_id=? AND status='completed'",
                         (user_id,)).fetchone()[0]
        failed = c.execute("SELECT COUNT(*) FROM runs WHERE user_id=? AND status='failed'",
                           (user_id,)).fetchone()[0]
    return done, failed, rows


def count_all_users():
    with _conn() as c:
        linked = c.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
        total_runs = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        completed = c.execute("SELECT COUNT(*) FROM runs WHERE status='completed'").fetchone()[0]
    return linked, total_runs, completed


def list_linked_users():
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT user_id FROM tokens").fetchall()]


# ------------------------------------------------------------------ quest engine
API = "https://discord.com/api/v9"

BASE_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://discord.com",
    "Referer": "https://discord.com/channels/@me",
    "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "X-Discord-Locale": "en-US",
    "X-Discord-Timezone": "America/New_York",
    "X-Super-Properties": (
        "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwic3lz"
        "dGVtX2xvY2FsZSI6ImVuLVVTIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxsYS81"
        "LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2"
        "IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEyMi4wLjAuMCBTYWZhcmkvNTM3LjM2"
        "IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTIyLjAuMC4wIiwib3NfdmVyc2lvbiI6IjEwIiwv"
        "cmVmZXJyZXIiOiIiLCJyZWZlcnJpbmdfZG9tYWluIjoiIiwicmVmZXJyZXJfY3VycmVu"
        "dCI6IiIsInJlZmVycmluZ19kb21haW5fY3VycmVudCI6IiIsInJlbGVhc2VfY2hhbm5l"
        "bCI6InN0YWJsZSIsImNsaWVudF9idWlsZF9udW1iZXIiOjI3MDM4OSwiY2xpZW50X2V2"
        "ZW50X3NvdXJjZSI6bnVsbH0="
    ),
}


class QuestEngine:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update(BASE_HEADERS)
        self.s.headers["Authorization"] = token.strip().strip('"').strip("'")

    def _req(self, method, url, **kw):
        last = None
        for _ in range(5):
            try:
                r = self.s.request(method, url, timeout=20, **kw)
            except requests.RequestException as e:
                last = e
                time.sleep(2)
                continue
            last = r
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 3))
                except Exception:
                    wait = 3.0
                time.sleep(wait + 0.5)
                continue
            return r
        return last

    def warmup(self):
        for u in ("https://discord.com/app", "https://discord.com/channels/@me"):
            try:
                self.s.get(u, timeout=15)
            except Exception:
                pass

    def login(self):
        r = self._req("GET", f"{API}/users/@me")
        if r is not None and r.status_code == 200:
            return r.json()
        return None

    def quests(self):
        r = self._req("GET", f"{API}/quests/@me")
        if r is None or r.status_code != 200:
            return []
        d = r.json()
        if isinstance(d, dict):
            return d.get("quests", [])
        return d if isinstance(d, list) else []

    @staticmethod
    def blob(q):
        cfg = q.get("config", {}) or {}
        return " ".join([
            json.dumps(cfg.get("rewards", [])),
            json.dumps(cfg.get("reward_config", {})),
            json.dumps(q.get("reward_config", {})),
            json.dumps(cfg.get("messages", {})),
        ]).lower()

    @classmethod
    def is_orb(cls, q):
        blob = cls.blob(q)
        if "orb" in blob:
            return True
        for rw in (q.get("config", {}) or {}).get("rewards", []) or []:
            if rw.get("type") in (4, "COLLECTIBLE", "collectible"):
                return True
        return False

    @staticmethod
    def name(q):
        cfg = q.get("config", {}) or {}
        return (cfg.get("messages", {}).get("quest_name")
                or cfg.get("application", {}).get("name")
                or q.get("id", "?"))

    @staticmethod
    def qtype(q):
        task = (q.get("config", {}) or {}).get("task_config", {}) or {}
        tasks = task.get("tasks", {}) or {}
        for k in ("WATCH_VIDEO", "PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP",
                  "PLAY_ACTIVITY", "ACHIEVEMENT_IN_ACTIVITY", "WATCH_VIDEO_ON_MOBILE"):
            if k in tasks:
                return k
        return "WATCH_VIDEO"

    @staticmethod
    def target(q):
        cfg = q.get("config", {}) or {}
        task = cfg.get("task_config", {}) or {}
        tasks = task.get("tasks", {}) or {}
        t = tasks.get(QuestEngine.qtype(q), {}) or {}
        return int(t.get("target") or task.get("target") or 900)

    def enroll(self, qid):
        r = self._req("POST", f"{API}/quests/{qid}/enroll")
        return r is not None and r.status_code in (200, 201, 204, 400, 409)

    def hb(self, qid, sec):
        r = self._req("POST", f"{API}/quests/{qid}/video-progress", json={"timestamp": sec})
        if r is None or r.status_code not in (200, 204):
            r = self._req("POST", f"{API}/quests/{qid}/video-progress",
                          json={"timestamp": sec, "stream_key": ""})
        try:
            return r.json() if (r is not None and r.content) else {}
        except ValueError:
            return {}

    def claim(self, qid):
        r = self._req("POST", f"{API}/quests/{qid}/claim-reward")
        return r is not None and r.status_code in (200, 201, 204)

    def complete(self, q):
        qid = q["id"]
        target = self.target(q)
        if not self.enroll(qid):
            return False
        t = 0
        done = False
        while t < target:
            t = min(t + 30, target)
            resp = self.hb(qid, t)
            if isinstance(resp, dict) and resp.get("completed_at"):
                done = True
                break
            time.sleep(2)
        time.sleep(2)
        return self.claim(qid) or done


# ------------------------------------------------------------------ bot
intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

_pending = set()
_running = {}
_cooldown_seconds = DEFAULT_COOLDOWN


def is_owner(user_id):
    return OWNER_ID != 0 and user_id == OWNER_ID


def owner_only():
    async def predicate(ctx):
        if not is_owner(ctx.author.id):
            raise commands.CheckFailure("Owner-only command.")
        return True
    return commands.check(predicate)


def _branded(title, color=0x5865F2, description=None):
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text=FOOTER)
    return e


@bot.event
async def on_ready():
    bar = "=" * 52
    print(bar)
    print(f"  {BOT_NAME}  v{VERSION}")
    print(f"  made by {AUTHOR}")
    print(f"  Storage: plaintext SQLite")
    print(f"  Owner ID: {OWNER_ID if OWNER_ID else 'NOT SET'}")
    print(bar)
    print(f"[+] Logged in as {bot.user} (id: {bot.user.id})")
    print(f"[+] Serving {len(bot.guilds)} guild(s)")
    print(f"[+] Prefix: {PREFIX}")
    print(bar)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return await ctx.reply(f"⛔ {error}")
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.reply(f"Missing argument. Try `{PREFIX}help`.")
    await ctx.reply(f"⚠️ `{type(error).__name__}: {error}`")


# ---------- ping ----------
@bot.command(name="ping")
async def ping_cmd(ctx):
    e = _branded("🏓 Pong", 0x57F287)
    e.add_field(name="Latency", value=f"{round(bot.latency * 1000)} ms")
    await ctx.reply(embed=e)


# ---------- about ----------
@bot.command(name="about")
async def about_cmd(ctx):
    e = _branded(BOT_NAME, 0x5865F2,
                 description=f"**Author:** {AUTHOR}\n**Version:** {VERSION}")
    e.add_field(name="Storage", value="Plaintext SQLite", inline=False)
    e.add_field(name="Prefix", value=f"`{PREFIX}`")
    e.add_field(name="Cooldown", value=f"{_cooldown_seconds}s (owner bypasses)")
    if OWNER_ID:
        e.add_field(name="Owner", value=f"<@{OWNER_ID}>")
    await ctx.reply(embed=e)


# ---------- help ----------
@bot.command(name="help")
async def help_cmd(ctx):
    e = _branded(BOT_NAME, 0x5865F2,
                 description=f"Made by **{AUTHOR}**  •  Prefix: `{PREFIX}`")
    cmds = [
        ("link", "Link your Discord account (via DM)"),
        ("unlink", "Remove your stored token"),
        ("status", "Check if you're linked"),
        ("quests", "List available orb quests"),
        ("questall", "Complete every orb quest"),
        ("stats", "Your completion history"),
        ("ping", "Latency check"),
        ("about", "About this bot"),
        ("help", "This message"),
    ]
    for name, desc in cmds:
        e.add_field(name=f"{PREFIX}{name}", value=desc, inline=False)

    if is_owner(ctx.author.id):
        e.add_field(name="— Owner —", value="\u200b", inline=False)
        owner_cmds = [
            ("owner", "Owner panel"),
            ("users", "List linked users"),
            ("setcd <sec>", "Set global cooldown"),
            ("say <text>", "Make the bot speak"),
            ("unlinkuser <id>", "Force-unlink a user"),
        ]
        for name, desc in owner_cmds:
            e.add_field(name=f"{PREFIX}{name}", value=desc, inline=False)

    await ctx.reply(embed=e)


# ---------- link ----------
@bot.command(name="link")
async def link_cmd(ctx):
    uid = ctx.author.id
    if get_token(uid):
        return await ctx.reply("Already linked. `*unlink` first to re-link.")
    if uid in _pending:
        return await ctx.reply("Already in progress — check your DMs.")
    _pending.add(uid)
    try:
        await ctx.reply("📩 Check your DMs.")
        try:
            dm = await ctx.author.create_dm()
            await dm.send(
                f"**{BOT_NAME}** — link your account\n\n"
                "1. Open `discord.com/app` in a browser (logged in)\n"
                "2. Press `F12` → **Network** tab\n"
                "3. Refresh the page\n"
                "4. Click any request to `discord.com/api/...`\n"
                "5. **Headers** → **Request Headers** → copy the `authorization` value\n\n"
                "**Paste it here.** Reply `cancel` to abort.\n"
                "_Stored plaintext. Use `*unlink` to wipe it anytime._"
            )
        except discord.Forbidden:
            _pending.discard(uid)
            return await ctx.reply("Can't DM you — enable DMs from server members.")

        def check(m):
            return m.author.id == uid and isinstance(m.channel, discord.DMChannel)

        try:
            msg = await bot.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            _pending.discard(uid)
            return await dm.send("Timed out.")

        c = msg.content.strip()
        if c.lower() == "cancel":
            _pending.discard(uid)
            return await dm.send("Cancelled.")
        if len(c) < 50 or " " in c:
            _pending.discard(uid)
            return await dm.send("That doesn't look like a token. Try `*link` again.")

        eng = QuestEngine(c)
        eng.warmup()
        user = await asyncio.to_thread(eng.login)
        if not user:
            _pending.discard(uid)
            return await dm.send("❌ Discord rejected that token. Re-copy and try again.")

        save_token(uid, c)
        _pending.discard(uid)
        await dm.send(f"✅ Linked as **{user['username']}**. Try `*quests` in the server.")
    except Exception as e:
        _pending.discard(uid)
        try:
            await ctx.author.send(f"Link failed: `{e}`")
        except Exception:
            pass


# ---------- unlink ----------
@bot.command(name="unlink")
async def unlink_cmd(ctx):
    if delete_token(ctx.author.id):
        await ctx.reply("🗑️ Unlinked.")
    else:
        await ctx.reply("You aren't linked.")


# ---------- status ----------
@bot.command(name="status")
async def status_cmd(ctx):
    linked = get_token(ctx.author.id) is not None
    tag = " 👑 owner" if is_owner(ctx.author.id) else ""
    await ctx.reply(("✅ Linked." if linked else "❌ Not linked. Run `*link`.") + tag)


# ---------- quests ----------
@bot.command(name="quests")
async def quests_cmd(ctx):
    token = get_token(ctx.author.id)
    if not token:
        return await ctx.reply("Not linked. `*link` first.")
    eng = QuestEngine(token)

    def _go():
        eng.warmup()
        if not eng.login():
            return None
        return eng.quests()

    qs = await asyncio.to_thread(_go)
    if qs is None:
        return await ctx.reply("Token dead — `*unlink` then `*link`.")
    orbs = [q for q in qs if eng.is_orb(q)]
    if not orbs:
        return await ctx.reply("No orb quests right now.")
    e = _branded("Orb quests available", 0x5865F2)
    for q in orbs[:15]:
        e.add_field(name=eng.name(q), value=f"{eng.qtype(q)} — {eng.target(q)}s", inline=False)
    await ctx.reply(embed=e)


# ---------- questall ----------
@bot.command(name="questall")
async def questall_cmd(ctx):
    uid = ctx.author.id
    token = get_token(uid)
    if not token:
        return await ctx.reply("Not linked. `*link` first.")

    owner = is_owner(uid)
    now = time.time()
    if not owner and uid in _running and now - _running[uid] < _cooldown_seconds:
        rem = int(_cooldown_seconds - (now - _running[uid]))
        return await ctx.reply(f"Cooldown — wait {rem}s.")

    _running[uid] = now
    msg = await ctx.reply("▶ Starting orb run…" + (" *(owner — no cooldown)*" if owner else ""))

    def _go():
        eng = QuestEngine(token)
        eng.warmup()
        user = eng.login()
        if not user:
            return {"error": "login_failed"}
        quests = [q for q in eng.quests() if eng.is_orb(q)]
        done, failed = [], []
        for q in quests:
            try:
                ok = eng.complete(q)
            except Exception:
                ok = False
            if not ok:
                try:
                    ok = eng.complete(q)
                except Exception:
                    ok = False
            (done if ok else failed).append(eng.name(q))
        return {"user": user["username"], "done": done, "failed": failed}

    try:
        res = await asyncio.to_thread(_go)
    except Exception as e:
        return await msg.edit(content=f"❌ {type(e).__name__}: `{e}`")

    if res.get("error") == "login_failed":
        return await msg.edit(content="❌ Token invalid. Re-link.")

    for n in res["done"]:
        log_run(uid, n, "completed")
    for n in res["failed"]:
        log_run(uid, n, "failed")

    e = _branded(f"Run finished — {res['user']}", 0x57F287)
    if res["done"]:
        e.add_field(name="✅ Completed", value="\n".join(f"• {n}" for n in res["done"])[:1000], inline=False)
    if res["failed"]:
        e.add_field(name="❌ Failed", value="\n".join(f"• {n}" for n in res["failed"])[:1000], inline=False)
    if not res["done"] and not res["failed"]:
        e.description = "No orb quests to run."
    await msg.edit(content=None, embed=e)


# ---------- stats ----------
@bot.command(name="stats")
async def stats_cmd(ctx):
    done, failed, rows = get_stats(ctx.author.id)
    e = _branded("Your stats", 0x5865F2)
    e.add_field(name="Completed", value=str(done))
    e.add_field(name="Failed", value=str(failed))
    if rows:
        lines = [f"{'✅' if s == 'completed' else '❌'} {q}" for q, s in rows]
        e.add_field(name="Recent", value="\n".join(lines)[:1000], inline=False)
    await ctx.reply(embed=e)


# ================================================================== OWNER

@bot.command(name="owner")
@owner_only()
async def owner_cmd(ctx):
    linked, total_runs, completed = count_all_users()
    e = _branded("👑 Owner Panel", 0xF1C40F)
    e.add_field(name="Linked users", value=str(linked))
    e.add_field(name="Total runs", value=str(total_runs))
    e.add_field(name="Completed", value=str(completed))
    e.add_field(name="Global cooldown", value=f"{_cooldown_seconds}s")
    e.add_field(name="Owner ID", value=str(OWNER_ID))
    e.add_field(name="Guilds", value=str(len(bot.guilds)))
    await ctx.reply(embed=e)


@bot.command(name="users")
@owner_only()
async def users_cmd(ctx):
    ids = list_linked_users()
    if not ids:
        return await ctx.reply("No linked users yet.")
    lines = []
    for uid in ids[:30]:
        try:
            u = await bot.fetch_user(uid)
            lines.append(f"• {u} (`{uid}`)")
        except Exception:
            lines.append(f"• `{uid}` (unknown)")
    e = _branded(f"Linked users ({len(ids)})", 0x5865F2)
    e.description = "\n".join(lines)
    await ctx.reply(embed=e)


@bot.command(name="setcd")
@owner_only()
async def setcd_cmd(ctx, seconds: int):
    global _cooldown_seconds
    if seconds < 0:
        return await ctx.reply("Must be >= 0.")
    _cooldown_seconds = seconds
    await ctx.reply(f"✅ Global cooldown set to {seconds}s.")


@bot.command(name="say")
@owner_only()
async def say_cmd(ctx, *, text: str):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(text)


@bot.command(name="unlinkuser")
@owner_only()
async def unlinkuser_cmd(ctx, user_id: int):
    if delete_token(user_id):
        await ctx.reply(f"🗑️ Unlinked `{user_id}`.")
    else:
        await ctx.reply(f"`{user_id}` isn't linked.")


# ================================================================== run
if __name__ == "__main__":
    try:
        bot.run(BOT_TOKEN)
    except discord.errors.LoginFailure:
        print("❌ BOT_TOKEN is invalid. Get a fresh one from the Dev Portal.")
    except discord.errors.PrivilegedIntentsRequired:
        print("❌ Enable 'Message Content Intent' in the Dev Portal → Bot tab.")
    except KeyboardInterrupt:
        print("\nbye.")
