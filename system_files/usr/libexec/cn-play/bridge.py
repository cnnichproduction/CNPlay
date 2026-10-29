#!/usr/bin/python3
"""CN-PLAY bridge: serves the UI and lets it list/launch Steam games on this machine.
Listens on 127.0.0.1 only. POST requests need the X-CN-Play header, which web pages
from other sites cannot send without a CORS preflight (we never answer one)."""
import glob, json, mimetypes, os, re, shutil, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7878
UI = Path(os.environ.get("CN_PLAY_UI", "/usr/share/cn-play/ui"))
DRY = bool(os.environ.get("CN_PLAY_DRY_RUN"))
HOME = Path(os.environ.get("CN_PLAY_HOME", Path.home()))
ROOTS = [HOME / ".local/share/Steam", HOME / ".steam/steam",
         HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
SKIP = ("Proton", "Steam Linux Runtime", "Steamworks Common", "Steam Controller")
ART = {}

def libraries():
    out = []
    for r in ROOTS:
        if not r.is_dir():
            continue
        out.append(r)
        vdf = r / "steamapps/libraryfolders.vdf"
        if vdf.is_file():
            out += [Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore"))]
    seen, uniq = set(), []
    for p in out:
        k = str(p.resolve()) if p.exists() else str(p)
        if k not in seen:
            seen.add(k); uniq.append(p)
    return uniq

def find_art(appid):
    for r in ROOTS:
        cache = r / "appcache/librarycache"
        old = cache / f"{appid}_library_600x900.jpg"
        if old.is_file():
            return str(old)
        hits = glob.glob(str(cache / appid / "**" / "library_600x900.jpg"), recursive=True)
        if hits:
            return hits[0]
    return None

def games():
    found = {}
    for lib in libraries():
        for m in glob.glob(str(lib / "steamapps/appmanifest_*.acf")):
            t = Path(m).read_text(errors="ignore")
            i = re.search(r'"appid"\s+"(\d+)"', t); n = re.search(r'"name"\s+"([^"]+)"', t)
            if i and n and not n.group(1).startswith(SKIP):
                found[i.group(1)] = n.group(1)
    out = []
    for appid, name in sorted(found.items(), key=lambda kv: kv[1].lower()):
        art = find_art(appid)
        ART[appid] = art
        out.append({"id": appid, "name": name, "art": f"/art/{appid}" if art else None})
    return out

def run(cmd):
    print("run:", cmd, flush=True)
    if not DRY:
        subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def steam_cmd(uri):
    if shutil.which("steam"):
        return ["steam", "-silent", uri]
    return ["flatpak", "run", "com.valvesoftware.Steam", "-silent", uri]

def nm(args, timeout=25):
    try:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr.strip()
    except FileNotFoundError:
        return 127, "", "nmcli not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"

def split_t(line):
    """Split one line of `nmcli -t` output on unescaped colons."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]; i += 2; continue
        if c == ":":
            out.append(cur); cur = ""
        else:
            cur += c
        i += 1
    out.append(cur)
    return out

def wifi_current(out):
    for f in map(split_t, out.splitlines()):
        if f[0] == "wifi" and len(f) > 2 and f[1] == "connected":
            return f[2]
    return ""

def wifi_status():
    rc, out, _ = nm(["-t", "-f", "TYPE,STATE,CONNECTION", "device"])
    if rc == 127:
        return {"avail": False, "cur": ""}
    return {"avail": True, "cur": wifi_current(out)}

def wifi_info():
    rc, out, _ = nm(["-t", "-f", "TYPE,STATE,CONNECTION", "device"])
    if rc == 127:
        return {"avail": False}
    if not any(split_t(l)[0] == "wifi" for l in out.splitlines()):
        return {"avail": True, "dev": False}
    on = nm(["-t", "radio", "wifi"])[1].strip() == "enabled"
    known = {split_t(l)[0] for l in nm(["-t", "-f", "NAME,TYPE", "connection", "show"])[1].splitlines()
             if l.endswith("802-11-wireless")}
    nets = {}
    if on:
        rows = nm(["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "no"])[1]
        for l in rows.splitlines():
            f = split_t(l)
            if len(f) < 4 or not f[1]:
                continue
            n = {"ssid": f[1], "signal": int(f[2] or 0), "sec": f[3] not in ("", "--"),
                 "known": f[1] in known, "active": f[0] == "*"}
            old = nets.get(f[1])
            if not old or n["active"] or (n["signal"] > old["signal"] and not old["active"]):
                nets[f[1]] = n
    lst = sorted(nets.values(), key=lambda n: (not n["active"], -n["signal"]))[:12]
    return {"avail": True, "dev": True, "on": on, "cur": wifi_current(out), "nets": lst}

def wifi_connect(ssid, pw):
    if not isinstance(ssid, str) or not ssid or len(ssid.encode()) > 32 or ssid.startswith("-"):
        return {"ok": False, "error": "Invalid network name"}
    if not isinstance(pw, str) or len(pw) > 63:
        return {"ok": False, "error": "Invalid password"}
    cmd = ["--wait", "20", "device", "wifi", "connect", ssid] + (["password", pw] if pw else [])
    rc, _, err = nm(cmd, 30)
    return {"ok": rc == 0, "error": "" if rc == 0 else (err or "Could not connect")}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)): body = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
    def do_GET(self):
        if not self.host_ok(): return self.send(403, {"error": "bad host"})
        p = self.path.split("?")[0]
        if p == "/api/ping": return self.send(200, {"ok": True})
        if p == "/api/games": return self.send(200, games())
        if p == "/api/wifi": return self.send(200, wifi_info())
        if p == "/api/wifi/status": return self.send(200, wifi_status())
        if p.startswith("/art/"):
            f = ART.get(p[5:]); 
            if f and os.path.isfile(f): return self.send(200, open(f, "rb").read(), "image/jpeg")
            return self.send(404, b"", "text/plain")
        f = (UI / ("index.html" if p == "/" else p.lstrip("/"))).resolve()
        if UI.resolve() in f.parents and f.is_file():
            return self.send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "application/octet-stream")
        self.send(404, {"error": "not found"})
    def do_POST(self):
        if not self.host_ok() or self.headers.get("X-CN-Play") != "1":
            return self.send(403, {"error": "forbidden"})
        p = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 4096))) if n else {}
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if m := re.fullmatch(r"/api/launch/(\d+)", p):
            run(steam_cmd(f"steam://rungameid/{m.group(1)}"))
        elif p == "/api/store": run(steam_cmd("steam://store"))
        elif p == "/api/power/reboot": run(["systemctl", "reboot"])
        elif p == "/api/power/poweroff": run(["systemctl", "poweroff"])
        elif p == "/api/system-settings":
            app = next((a for a in ("systemsettings", "gnome-control-center") if shutil.which(a)), None)
            if app: run([app])
        elif p == "/api/quit": run(["pkill", "-f", "cn-play-ui"])
        elif p == "/api/wifi/connect": return self.send(200, wifi_connect(body.get("ssid"), body.get("password", "")))
        elif p in ("/api/wifi/radio/on", "/api/wifi/radio/off"): nm(["radio", "wifi", p.rsplit("/", 1)[1]], 10)
        elif p == "/api/wifi/scan": nm(["device", "wifi", "rescan"], 10)
        else: return self.send(404, {"error": "unknown action"})
        self.send(200, {"ok": True})

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
