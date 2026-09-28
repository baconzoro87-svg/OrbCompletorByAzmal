# bot.py
"""
Best Quest Completer v7 — production build
Fixed async plumbing, per-user concurrency, live progress, cancel,
graceful shutdown, full browser fingerprint.
"""

import os
import time
import json
import asyncio
import sqlite3
import signal
import sys
from asyncio import Semaphore
from typing import Optional, Dict, List, Tuple

import discord
from discord.ext import commands
from dotenv import load_dotenv
import aiohttp

# ================================================================ config
load_dotenv()

BOT_NAME = "Best Quest Completer"
AUTHOR = "Azmal"
VERSION = "7.0"
FOOTER = f"{BOT_NAME} — by {AUTHOR}"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
DB_PATH = os.environ.get("DB_PATH", "tokens.db")
PREFIX = "*"
DEFAULT_COOLDOWN = 120
POOL_SIZE = 4
MAX_CONCURRENT_QUESTS_GLOBAL = 12
MAX_CONCURRENT_QUESTS_PER_USER = 3

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing from .env")

# ================================================================ async db pool
class AsyncDBPool:
    """Persistent connection pool with proper locking and initialization."""

    def __init__(self, path: str, pool_size: int = POOL_SIZE):
        self.path = path
        self.pool_size = pool_size
        self._conns: List[sqlite3.Connection] = []
        self._queue: Optional[asyncio.Queue] = None
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._closed = False

    async def _init_pool(self):
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            self._queue = asyncio.Queue(maxsize=self.pool_size)
            for _ in range(self.pool_size):
                conn = sqlite3.connect(
                    self.path,
                    check_same_thread=False,
                    timeout=10,
                    isolation_level=None,  # autocommit; we control tx explicitly
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("""CREATE TABLE IF NOT EXISTS tokens (
                    user_id   INTEGER PRIMARY KEY,
                    token     TEXT NOT NULL,
                    linked_at INTEGER NOT NULL
                )""")
                conn.execute("""CREATE TABLE IF NOT EXISTS runs (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    quest   TEXT NOT NULL,
                    status  TEXT NOT NULL,
                    ts      INTEGER NOT NULL
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id, ts DESC)")
                self._conns.append(conn)
                await self._queue.put(conn)
            self._initialized = True

    async def _acquire(self) -> sqlite3.Connection:
        await self._init_pool()
        return await self._queue.get()

    def _release(self, conn: sqlite3.Connection):
        if not self._closed:
            try:
                self._queue.put_nowait(conn)
            except asyncio.QueueFull:
                pass

    async def fetch(self, query: str, params: Tuple = ()) -> List[Tuple]:
        conn = await self._acquire()
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, lambda: conn.execute(query, params).fetchall()
            )
        finally:
            self._release(conn)

    async def fetch_one(self, query: str, params: Tuple = ()) -> Optional[Tuple]:
        rows = await self.fetch(query, params)
        return rows[0] if rows else None

    async def execute(self, query: str, params: Tuple = ()) -> int:
        conn = await self._acquire()
        try:
            cur = await asyncio.get_running_loop().run_in_executor(
                None, lambda: conn.execute(query, params)
            )
            return cur.rowcount
        finally:
            self._release(conn)

    async def close(self):
        self._closed = True
        for conn in self._conns:
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()


db = AsyncDBPool(DB_PATH)

# ================================================================ quest engine
API = "https://discord.com/api/v9"

SUPER_PROPS = (
    "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwic3lz"
    "dGVtX2xvY2FsZSI6ImVuLVVTIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxsYS81"
    "LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2"
    "IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEyMi4wLjAuMCBTYWZhcmkvNTM3LjM2"
    "IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTIyLjAuMC4wIiwib3NfdmVyc2lvbiI6IjEwIiwv"
    "cmVmZXJyZXIiOiIiLCJyZWZlcnJpbmdfZG9tYWluIjoiIiwicmVmZXJyZXJfY3VycmVu"
    "dCI6IiIsInJlZmVycmluZ19kb21haW5fY3VycmVudCI6IiIsInJlbGVhc2VfY2hhbm5l"
    "bCI6InN0YWJsZSIsImNsaWVudF9idWlsZF9udW1iZXIiOjI3MDM4OSwiY2xpZW50X2V2"
    "ZW50X3NvdXJjZSI6bnVsbH0="
)

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
    "X-Super-Properties": SUPER_PROPS,
}


class QuestEngine:
    """Per-token engine. Owns its own session + cookie jar."""

    def __init__(self, token: str, session: aiohttp.ClientSession):
        self.token = token.strip().strip('"').strip("'")
        self.session = session
        self._user_cache: Optional[Dict] = None
        self._warmed = False

    async def _req(self, method: str, url: str, **kw) -> Tuple[Optional[int], Optional[dict]]:
        """Returns (status, body). Body read inside the context so it survives."""
        headers = {**BASE_HEADERS, "Authorization": self.token}
        backoff = 0.6
        for attempt in range(5):
            try:
                async with self.session.request(
                    method, url, headers=headers, timeout=aiohttp.ClientTimeout(total=20), **kw
                ) as r:
                    # 429 → respect retry_after and try again
                    if r.status == 429:
                        try:
                            wait = float(r.headers.get("retry-after") or 1)
                        except Exception:
                            wait = 1.0
                        await asyncio.sleep(min(wait + 0.2, 30))
                        continue

                    # 5xx → backoff and retry
                    if r.status >= 500:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 1.7, 12)
                        continue

                    # Read body while the response is still open
                    body = None
                    if r.content_type == "application/json":
                        try:
                            body = await r.json()
                        except Exception:
                            body = None
                    else:
                        try:
                            txt = await r.text()
                            body = txt if txt else None
                        except Exception:
                            body = None

                    return r.status, body
            except asyncio.TimeoutError:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.7, 12)
            except aiohttp.ClientError:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.7, 12)
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.7, 12)
        return None, None

    async def warmup(self):
        """Grab cookies before hitting quest routes. Discord rejects cold sessions."""
        if self._warmed:
            return
        try:
            async with self.session.get("https://discord.com/app",
                                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                await r.read()
        except Exception:
            pass
        try:
            async with self.session.get("https://discord.com/channels/@me",
                                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                await r.read()
        except Exception:
            pass
        self._warmed = True

    async def login(self) -> Optional[Dict]:
        if self._user_cache:
            return self._user_cache
        status, body = await self._req("GET", f"{API}/users/@me")
        if status == 200 and isinstance(body, dict):
            self._user_cache = body
            return body
        return None

    async def quests(self) -> List[Dict]:
        status, body = await self._req("GET", f"{API}/quests/@me")
        if status != 200 or body is None:
            return []
        if isinstance(body, dict):
            return body.get("quests", []) or []
        return body if isinstance(body, list) else []

    @staticmethod
    def is_orb(q: Dict) -> bool:
        cfg = q.get("config", {}) or {}
        blob = json.dumps({
            "rewards": cfg.get("rewards", []),
            "reward_config": cfg.get("reward_config", {}),
            "top_reward": q.get("reward_config", {}),
            "messages": cfg.get("messages", {}),
        }).lower()
        if "orb" in blob:
            return True
        for rw in cfg.get("rewards", []) or []:
            if rw.get("type") in (4, "COLLECTIBLE", "collectible"):
                return True
        return False

    @staticmethod
    def name(q: Dict) -> str:
        cfg = q.get("config", {}) or {}
        return (
            cfg.get("messages", {}).get("quest_name")
            or cfg.get("application", {}).get("name")
            or q.get("id", "?")
        )

    @staticmethod
    def target(q: Dict) -> int:
        cfg = q.get("config", {}) or {}
        task = cfg.get("task_config", {}) or {}
        tasks = task.get("tasks", {}) or {}
        # pick whichever task key exists
        for key in ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE", "PLAY_ON_DESKTOP",
                    "STREAM_ON_DESKTOP", "PLAY_ACTIVITY"):
            t = tasks.get(key, {}) or {}
            if "target" in t:
                try:
                    return int(t["target"])
                except (TypeError, ValueError):
                    pass
        try:
            return int(task.get("target") or 900)
        except (TypeError, ValueError):
            return 900

    async def _claim(self, qid: str) -> bool:
        status, _ = await self._req("POST", f"{API}/quests/{qid}/claim-reward")
        return status in (200, 201, 204)

    async def complete(self, quest: Dict, on_step=None) -> bool:
        qid = quest["id"]
        target = self.target(quest)

        status, _ = await self._req("POST", f"{API}/quests/{qid}/enroll")
        if status not in (200, 201, 204, 400, 409):
            return False

        elapsed = 0
        while elapsed < target:
            await asyncio.sleep(2)
            elapsed = min(elapsed + 30, target)
            status, resp = await self._req(
                "POST",
                f"{API}/quests/{qid}/video-progress",
                json={"timestamp": elapsed},
            )
            if on_step:
                try:
                    on_step(elapsed, target)
                except Exception:
                    pass
            if status in (200, 204) and isinstance(resp, dict) and resp.get("completed_at"):
                await asyncio.sleep(0.6)
                return await self._claim(qid)

        await asyncio.sleep(0.8)
        return await self._claim(qid)


# ================================================================ bot core
intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

_pending: set = set()
_running: Dict[int, float] = {}          # user_id -> run start ts
_active_tasks: Dict[int, asyncio.Task] = {}  # user_id -> task
_cooldown_seconds = DEFAULT_COOLDOWN

_session: Optional[aiohttp.ClientSession] = None
_global_sem = Semaphore(MAX_CONCURRENT_QUESTS_GLOBAL)
_user_sems: Dict[int, Semaphore] = {}


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=64, limit_per_host=12, ttl_dns_cache=300)
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=45, connect=15),
            connector=connector,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
    return _session


def user_sem(uid: int) -> Semaphore:
    if uid not in _user_sems:
        _user_sems[uid] = Semaphore(MAX_CONCURRENT_QUESTS_PER_USER)
    return _user_sems[uid]


def is_owner(uid: int) -> bool:
    return OWNER_ID != 0 and uid == OWNER_ID


def owner_only():
    async def check(ctx):
        if not is_owner(ctx.author.id):
            raise commands.CheckFailure("Owner-only.")
        return True
    return commands.check(check)


def _branded(title: str, color: int = 0x5865F2, desc: Optional[str] = None) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=color)
    e.set_footer(text=FOOTER)
    return e


def _sanitize_token(raw: str) -> Optional[str]:
    t = raw.strip().strip('"').strip("'")
    if len(t) < 50 or " " in t or "\n" in t:
        return None
    # Discord user tokens are base64-ish, three segments split by dots
    if t.count(".") < 2:
        return None
    return t


# ================================================================ events
@bot.event
async def on_ready():
    bar = "=" * 54
    print(bar)
    print(f"  {BOT_NAME}  v{VERSION}")
    print(f"  made by {AUTHOR}")
    print(f"  Storage: async SQLite pool ({POOL_SIZE} conns, WAL)")
    print(f"  Global quest concurrency: {MAX_CONCURRENT_QUESTS_GLOBAL}")
    print(f"  Per-user concurrency:     {MAX_CONCURRENT_QUESTS_PER_USER}")
    print(f"  Owner ID: {OWNER_ID if OWNER_ID else 'NOT SET'}")
    print(bar)
    print(f"[+] {bot.user} (id: {bot.user.id})")
    print(f"[+] {len(bot.guilds)} guild(s)")
    print(f"[+] Prefix: {PREFIX}")
    print(bar)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return await ctx.reply(f"⛔ {error}")
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.reply(f"Missing arg. Try `{PREFIX}help`.")
    await ctx.reply(f"⚠️ `{type(error).__name__}: {error}`")


@bot.event
async def on_disconnect():
    global _session
    if _session and not _session.closed:
        try:
            await _session.close()
        except Exception:
            pass
        _session = None


# ================================================================ user commands
@bot.command(name="ping")
async def ping_cmd(ctx):
    e = _branded("🏓 Pong", 0x57F287)
    e.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms")
    await ctx.reply(embed=e)


@bot.command(name="about")
async def about_cmd(ctx):
    e = _branded(BOT_NAME, 0x5865F2, f"**Author:** {AUTHOR}\n**Version:** {VERSION}")
    e.add_field(name="Storage", value="Plaintext async SQLite", inline=False)
    e.add_field(name="Prefix", value=f"`{PREFIX}`")
    if OWNER_ID:
        e.add_field(name="Owner", value=f"<@{OWNER_ID}>")
    await ctx.reply(embed=e)


@bot.command(name="help")
async def help_cmd(ctx):
    e = _branded(BOT_NAME, 0x5865F2, f"made by **{AUTHOR}**  •  Prefix: `{PREFIX}`")
    cmds = [
        ("link", "Link your Discord account"),
        ("unlink", "Remove your token"),
        ("quests", "List orb quests"),
        ("questall", "Complete all orbs"),
        ("cancel", "Abort your active run"),
        ("stats", "Your history"),
        ("status", "Link status"),
        ("ping", "Latency"),
        ("about", "About"),
    ]
    for name, desc in cmds:
        e.add_field(name=f"{PREFIX}{name}", value=desc, inline=False)

    if is_owner(ctx.author.id):
        e.add_field(name="— Owner —", value="\u200b", inline=False)
        for name, desc in [
            ("owner", "Owner panel"),
            ("users", "Linked users"),
            ("setcd <sec>", "Set cooldown"),
            ("say <text>", "Send message"),
            ("unlinkuser <id>", "Force-unlink"),
        ]:
            e.add_field(name=f"{PREFIX}{name}", value=desc, inline=False)

    await ctx.reply(embed=e)


@bot.command(name="link")
async def link_cmd(ctx):
    uid = ctx.author.id
    if await db.fetch_one("SELECT 1 FROM tokens WHERE user_id=?", (uid,)):
        return await ctx.reply("Already linked. `*unlink` first.")
    if uid in _pending:
        return await ctx.reply("In progress — check DMs.")

    # try DM first, before marking pending
    try:
        dm = await ctx.author.create_dm()
        await dm.send(
            f"**{BOT_NAME}** — link your account\n\n"
            "1. Open `discord.com/app` (logged in)\n"
            "2. Press `F12` → **Network** tab\n"
            "3. Refresh\n"
            "4. Click any `discord.com/api/...` request\n"
            "5. **Headers** → **Request Headers** → copy `authorization`\n\n"
            "**Paste here.** Reply `cancel` to abort.\n"
            "_Token stored plaintext. `*unlink` to wipe._"
        )
    except discord.Forbidden:
        return await ctx.reply("Can't DM you — enable DMs from server members.")

    _pending.add(uid)
    await ctx.reply("📩 Check your DMs.")

    def check(m):
        return m.author.id == uid and isinstance(m.channel, discord.DMChannel)

    try:
        msg = await bot.wait_for("message", check=check, timeout=300)
    except asyncio.TimeoutError:
        _pending.discard(uid)
        try:
            await dm.send("⏱️ Timed out.")
        except Exception:
            pass
        return

    raw = msg.content.strip()
    if raw.lower() == "cancel":
        _pending.discard(uid)
        return await dm.send("Cancelled.")

    token = _sanitize_token(raw)
    if not token:
        _pending.discard(uid)
        return await dm.send("Invalid token format. Try `*link` again.")

    session = await get_session()
    eng = QuestEngine(token, session)
    await eng.warmup()
    user = await eng.login()
    if not user:
        _pending.discard(uid)
        return await dm.send("❌ Token rejected. Re-copy and try.")

    await db.execute(
        "INSERT INTO tokens (user_id, token, linked_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET token=excluded.token, linked_at=excluded.linked_at",
        (uid, token, int(time.time())),
    )
    _pending.discard(uid)
    await dm.send(f"✅ Linked as **{user['username']}**. Try `*questall` in the server.")


@bot.command(name="unlink")
async def unlink_cmd(ctx):
    affected = await db.execute("DELETE FROM tokens WHERE user_id=?", (ctx.author.id,))
    if affected:
        await ctx.reply("🗑️ Unlinked.")
    else:
        await ctx.reply("Not linked.")


@bot.command(name="status")
async def status_cmd(ctx):
    linked = await db.fetch_one("SELECT 1 FROM tokens WHERE user_id=?", (ctx.author.id,))
    tag = " 👑" if is_owner(ctx.author.id) else ""
    running = " 🏃 running" if ctx.author.id in _active_tasks else ""
    await ctx.reply(("✅ Linked" if linked else "❌ Not linked — `*link`") + tag + running)


@bot.command(name="quests")
async def quests_cmd(ctx):
    row = await db.fetch_one("SELECT token FROM tokens WHERE user_id=?", (ctx.author.id,))
    if not row:
        return await ctx.reply("Not linked. `*link` first.")

    session = await get_session()
    eng = QuestEngine(row[0], session)
    await eng.warmup()
    if not await eng.login():
        return await ctx.reply("Token dead — `*unlink` then `*link`.")

    qs = await eng.quests()
    orbs = [q for q in qs if eng.is_orb(q)]
    if not orbs:
        return await ctx.reply("No orb quests available right now.")

    e = _branded("Orb quests available", 0x5865F2)
    for q in orbs[:15]:
        e.add_field(name=eng.name(q), value=f"`{eng.target(q)}s`", inline=False)
    await ctx.reply(embed=e)


@bot.command(name="cancel")
async def cancel_cmd(ctx):
    uid = ctx.author.id
    task = _active_tasks.get(uid)
    if not task or task.done():
        return await ctx.reply("No active run to cancel.")
    task.cancel()
    _active_tasks.pop(uid, None)
    _running.pop(uid, None)
    await ctx.reply("🛑 Cancelled.")


@bot.command(name="questall")
async def questall_cmd(ctx):
    uid = ctx.author.id
    row = await db.fetch_one("SELECT token FROM tokens WHERE user_id=?", (uid,))
    if not row:
        return await ctx.reply("Not linked. `*link` first.")

    owner = is_owner(uid)
    now = time.time()

    # cooldown
    if not owner and uid in _running and now - _running[uid] < _cooldown_seconds:
        rem = int(_cooldown_seconds - (now - _running[uid]))
        return await ctx.reply(f"Cooldown — wait {rem}s.")

    # one run per user
    if uid in _active_tasks and not _active_tasks[uid].done():
        return await ctx.reply("Already running. `*cancel` to abort.")

    _running[uid] = now
    msg = await ctx.reply("▶ Starting…" + (" *(owner — no cooldown)*" if owner else ""))

    task = asyncio.create_task(_run_all(uid, row[0], msg, owner))
    _active_tasks[uid] = task
    try:
        await task
    finally:
        _active_tasks.pop(uid, None)


async def _run_all(uid: int, token: str, msg: discord.Message, owner: bool):
    session = await get_session()
    eng = QuestEngine(token, session)

    try:
        await eng.warmup()
        user = await eng.login()
        if not user:
            return await msg.edit(content="❌ Token dead. Re-link.")

        qs = await eng.quests()
        orbs = [q for q in qs if eng.is_orb(q)]
        if not orbs:
            return await msg.edit(content="No orb quests to run.")

        total = len(orbs)
        done, failed = [], []
        user_semaphore = user_sem(uid)

        async def run_one(q):
            async with _global_sem, user_semaphore:
                name = eng.name(q)
                try:
                    ok = await eng.complete(q)
                    if not ok:
                        ok = await eng.complete(q)  # one retry
                except asyncio.CancelledError:
                    raise
                except Exception:
                    ok = False
                return name, ok

        # kick off all quests (throttled by semaphores)
        tasks = [asyncio.create_task(run_one(q)) for q in orbs]

        # live progress loop
        completed_now = 0
        while any(not t.done() for t in tasks):
            completed_now = sum(1 for t in tasks if t.done())
            try:
                await msg.edit(content=f"▶ Running… {completed_now}/{total}")
            except Exception:
                pass
            await asyncio.sleep(3)

        results = await asyncio.gather(*tasks, return_exceptions=False)
        for name, ok in results:
            await db.execute(
                "INSERT INTO runs (user_id, quest, status, ts) VALUES (?, ?, ?, ?)",
                (uid, name, "completed" if ok else "failed", int(time.time())),
            )
            (done if ok else failed).append(name)

        e = _branded(f"Run finished — {user['username']}", 0x57F287)
        e.description = f"**{len(done)}/{total}** completed"
        if done:
            e.add_field(name="✅ Completed", value="\n".join(f"• {n}" for n in done)[:1000], inline=False)
        if failed:
            e.add_field(name="❌ Failed", value="\n".join(f"• {n}" for n in failed)[:1000], inline=False)
        await msg.edit(content=None, embed=e)

    except asyncio.CancelledError:
        try:
            await msg.edit(content="🛑 Cancelled.")
        except Exception:
            pass
        raise


@bot.command(name="stats")
async def stats_cmd(ctx):
    uid = ctx.author.id
    rows = await db.fetch(
        "SELECT quest, status FROM runs WHERE user_id=? ORDER BY ts DESC LIMIT 10",
        (uid,),
    )
    d = await db.fetch_one(
        "SELECT COUNT(*) FROM runs WHERE user_id=? AND status='completed'", (uid,)
    )
    f = await db.fetch_one(
        "SELECT COUNT(*) FROM runs WHERE user_id=? AND status='failed'", (uid,)
    )
    e = _branded("Your stats", 0x5865F2)
    e.add_field(name="✅ Completed", value=str(d[0] if d else 0))
    e.add_field(name="❌ Failed", value=str(f[0] if f else 0))
    if rows:
        lines = [f"{'✅' if s == 'completed' else '❌'} {q}" for q, s in rows]
        e.add_field(name="Recent", value="\n".join(lines)[:1000], inline=False)
    await ctx.reply(embed=e)


# ================================================================ owner commands
@bot.command(name="owner")
@owner_only()
async def owner_cmd(ctx):
    linked = await db.fetch_one("SELECT COUNT(*) FROM tokens")
    total_runs = await db.fetch_one("SELECT COUNT(*) FROM runs")
    completed = await db.fetch_one("SELECT COUNT(*) FROM runs WHERE status='completed'")
    e = _branded("👑 Owner Panel", 0xF1C40F)
    e.add_field(name="Linked users", value=str(linked[0] if linked else 0))
    e.add_field(name="Total runs", value=str(total_runs[0] if total_runs else 0))
    e.add_field(name="Completed", value=str(completed[0] if completed else 0))
    e.add_field(name="Cooldown", value=f"{_cooldown_seconds}s")
    e.add_field(name="Guilds", value=str(len(bot.guilds)))
    e.add_field(name="Active runs", value=str(len(_active_tasks)))
    await ctx.reply(embed=e)


@bot.command(name="setcd")
@owner_only()
async def setcd_cmd(ctx, seconds: int):
    global _cooldown_seconds
    if seconds < 0:
        return await ctx.reply("Must be >= 0.")
    _cooldown_seconds = seconds
    await ctx.reply(f"✅ Cooldown set to {seconds}s.")


@bot.command(name="say")
@owner_only()
async def say_cmd(ctx, *, text: str):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(text)


@bot.command(name="users")
@owner_only()
async def users_cmd(ctx):
    ids = await db.fetch("SELECT user_id FROM tokens LIMIT 30")
    if not ids:
        return await ctx.reply("No linked users.")
    e = _branded(f"Linked users ({len(ids)})", 0x5865F2)
    lines = []
    for (uid,) in ids:
        try:
            u = await bot.fetch_user(uid)
            lines.append(f"• {u} (`{uid}`)")
        except Exception:
            lines.append(f"• `{uid}` (unknown)")
    e.description = "\n".join(lines)
    await ctx.reply(embed=e)


@bot.command(name="unlinkuser")
@owner_only()
async def unlinkuser_cmd(ctx, user_id: int):
    affected = await db.execute("DELETE FROM tokens WHERE user_id=?", (user_id,))
    if affected:
        await ctx.reply(f"🗑️ Unlinked `{user_id}`.")
    else:
        await ctx.reply(f"`{user_id}` not linked.")


# ================================================================ lifecycle
async def _shutdown():
    global _session
    # cancel active tasks
    for uid, t in list(_active_tasks.items()):
        t.cancel()
    if _active_tasks:
        await asyncio.gather(*_active_tasks.values(), return_exceptions=True)
    # close aiohttp session
    if _session and not _session.closed:
        try:
            await _session.close()
        except Exception:
            pass
        _session = None
    # close db pool
    await db.close()
    print("[shutdown] clean.")


def _handle_sigterm():
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_shutdown())
    except Exception:
        pass


if __name__ == "__main__":
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: _handle_sigterm())
            except Exception:
                pass
        bot.run(BOT_TOKEN)
    except discord.errors.LoginFailure:
        print("❌ Invalid BOT_TOKEN.")
    except discord.errors.PrivilegedIntentsRequired:
        print("❌ Enable 'Message Content Intent' in the Dev Portal → Bot tab.")
    finally:
        try:
            asyncio.run(_shutdown())
        except Exception:
            pass
