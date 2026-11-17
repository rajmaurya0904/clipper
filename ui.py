"""Web UI for the clipper.  python ui.py  ->  opens http://127.0.0.1:8765

Runs clip.py / live.py as child processes, streams their progress to the page,
and serves finished clips. Standard library only.
"""
import json, os, re, signal, subprocess, sys, threading, time, uuid, webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "clips"
PAGE = ROOT / "web" / "index.html"
PORT = int(os.getenv("CLIPPER_PORT", "8765"))
WIN = os.name == "nt"

num = lambda lo, hi, t=float: lambda v: _range(t(v), lo, hi)
def _range(v, lo, hi):
    if not lo <= v <= hi: raise ValueError(f"must be between {lo} and {hi}")
    return v
def _match(rx, msg):
    def check(v):
        if not re.fullmatch(rx, str(v)): raise ValueError(msg)
        return str(v)
    return check

STYLE = {  # field -> (cli flag, validator); shared by both modes
    "model": ("--model", _match(r"tiny|base|small|medium|large-v3", "unknown model")),
    "font": ("--font", _match(r"[\w \-]{1,40}", "font name: letters, spaces, dashes only")),
    "color": ("--color", _match(r"[0-9A-Fa-f]{6}(,[0-9A-Fa-f]{6}){0,7}", "colours must be RRGGBB")),
    "words": ("--words", num(1, 6, int)),
}
OPTS = {
    "video": {"clips": ("--clips", num(1, 20, int)), "min": ("--min", num(5, 600)),
              "max": ("--max", num(5, 600)), **STYLE},
    "live": {"before": ("--before", num(5, 300)), "after": ("--after", num(0, 120)),
             "ratio": ("--ratio", num(1.2, 20)), "cooldown": ("--cooldown", num(10, 3600)), **STYLE},
}
SCRIPT = {"video": "clip.py", "live": "live.py"}


def build_argv(kind, o):
    """Form fields -> argv for clip.py / live.py. Everything is validated; no shell involved."""
    if kind not in OPTS: raise ValueError("unknown mode")
    url = str(o.get("url", "")).strip()
    if not re.fullmatch(r"https?://\S+", url): raise ValueError("Paste a full link starting with https://")
    argv = [sys.executable, "-u", str(ROOT / SCRIPT[kind]), url]
    for k, (flag, check) in OPTS[kind].items():
        if o.get(k) in (None, ""): continue
        try: argv += [flag, str(check(o[k]))]
        except (ValueError, TypeError) as e: raise ValueError(f"{k}: {e}")
    if kind == "video" and float(o.get("min") or 20) > float(o.get("max") or 60):
        raise ValueError("shortest clip length is longer than the longest")
    if o.get("no_crop"): argv.append("--no-crop")
    return argv


LIVE_RX = re.compile(r"chat ([\d.]+)/s\s+usual ([\d.]+)/s\s+clips (\d+)")
STEP_RX = re.compile(r"\[(\d+)/(\d+)\]")


class Job:
    def __init__(self, kind, url, argv, env):
        self.id, self.kind, self.url = uuid.uuid4().hex[:8], kind, url
        self.lines, self.status, self.started = deque(maxlen=400), "running", time.time()
        self.stats, self.stage, self.step = {}, "starting...", None
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WIN else {"start_new_session": True}
        self.proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                     errors="replace", bufsize=1, **flags)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:  # text mode turns live.py's \r status updates into lines too
            line = raw.strip()
            if not line or "WARN:" in line or "%|" in line: continue  # skip model-download bars / cv2 noise
            self.lines.append(line)
            if m := LIVE_RX.search(line):
                self.stats = {"rate": float(m[1]), "usual": float(m[2]), "clips": int(m[3])}
                self.stage = "watching chat"
            elif m := STEP_RX.search(line):
                self.step, self.stage = [int(m[1]), int(m[2])], f"making clip {m[1]} of {m[2]}"
            elif line.startswith(">> SPIKE"):
                self.stage = "spike! making a clip..."
            elif line.startswith("->"):
                self.stage = "clip saved: " + Path(line[2:].strip()).name
            elif not line.startswith(("[h264", "[hevc")):
                self.stage = line.replace(" (Ctrl+C to stop)", "")[:120]
        code = self.proc.wait()
        if self.status == "stopping": self.status = "stopped"
        else: self.status = "done" if code == 0 else "failed"

    def stop(self):
        if self.proc.poll() is not None: return
        self.status = "stopping"
        try:  # polite first: live.py finishes the clip it's on
            self.proc.send_signal(signal.CTRL_BREAK_EVENT) if WIN else os.killpg(self.proc.pid, signal.SIGINT)
        except OSError: pass
        threading.Thread(target=self._force_after, args=(180,), daemon=True).start()

    def _force_after(self, secs):
        try: self.proc.wait(secs)
        except subprocess.TimeoutExpired: self.kill()

    def kill(self):
        if self.proc.poll() is not None: return
        if WIN: subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)
        else: os.killpg(self.proc.pid, signal.SIGKILL)

    def view(self):
        return {"id": self.id, "kind": self.kind, "url": self.url, "status": self.status,
                "stage": self.stage, "step": self.step, "stats": self.stats,
                "started": self.started, "log": list(self.lines)[-80:]}


JOBS: dict[str, Job] = {}


def clips():
    if not OUT.exists(): return []
    fs = sorted((f for f in OUT.glob("*.mp4") if not f.name.startswith(".")),
                key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"name": f.name, "size": f.stat().st_size, "time": f.stat().st_mtime} for f in fs]


def clip_path(name):
    """Only plain file names inside clips/ -- no ../ tricks."""
    if Path(name).name != name or not name.endswith(".mp4") or name.startswith("."): return None
    p = OUT / name
    return p if p.is_file() else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body=b"", ctype="application/json", extra=()):
        if isinstance(body, (dict, list)): body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra: self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)

    def _local_only(self):
        # DNS-rebinding guard: only answer requests addressed to this machine
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            self._send(403, {"error": "local access only"}); return False
        return True

    def _json_body(self):
        # requiring JSON makes browsers preflight cross-site requests, which we never approve
        if "application/json" not in (self.headers.get("Content-Type") or ""): return None
        n = int(self.headers.get("Content-Length") or 0)
        try: return json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except json.JSONDecodeError: return None

    def do_GET(self):
        if not self._local_only(): return
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(200, {"jobs": [j.view() for j in JOBS.values()], "clips": clips(),
                                    "claude": bool(os.getenv("ANTHROPIC_API_KEY"))})
        if path.startswith("/clips/"):
            p = clip_path(path[7:])
            return self._file(p) if p else self._send(404, {"error": "no such clip"})
        self._send(404, {"error": "not found"})

    do_HEAD = do_GET

    def _file(self, p):
        """Serve a clip with HTTP Range support so the browser can seek in the video."""
        size = p.stat().st_size
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        start, end, code = 0, size - 1, 200
        if m and (m[1] or m[2]):
            start, end = (int(m[1]), int(m[2]) if m[2] else size - 1) if m[1] else (size - int(m[2]), size - 1)
            start, end, code = max(0, start), min(end, size - 1), 206
            if start > end: return self._send(416, b"", extra=[("Content-Range", f"bytes */{size}")])
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD": return
        with open(p, "rb") as f:
            f.seek(start); left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk: break
                try: self.wfile.write(chunk)
                except (ConnectionError, OSError): return  # viewer seeked / closed the tab
                left -= len(chunk)

    def do_POST(self):
        if not self._local_only(): return
        body = self._json_body()
        if body is None: return self._send(415, {"error": "send JSON"})
        parts = self.path.strip("/").split("/")
        if parts == ["api", "jobs"]:
            try: argv = build_argv(body.get("kind"), body)
            except ValueError as e: return self._send(400, {"error": str(e)})
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            if key := str(body.get("api_key") or "").strip(): env["ANTHROPIC_API_KEY"] = key  # memory only
            job = Job(body["kind"], body["url"].strip(), argv, env)
            JOBS[job.id] = job
            return self._send(200, {"id": job.id})
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and (job := JOBS.get(parts[2])):
            if parts[3] == "stop": job.stop(); return self._send(200, {"ok": True})
            if parts[3] == "dismiss" and job.proc.poll() is not None:
                JOBS.pop(job.id); return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._local_only(): return
        if self._json_body() is None: return self._send(415, {"error": "send JSON"})
        if self.path.startswith("/api/clips/") and (p := clip_path(self.path[11:])):
            p.unlink(); return self._send(200, {"ok": True})
        self._send(404, {"error": "no such clip"})


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Clipper UI running at {url}  (Ctrl+C to quit)")
    if "--no-browser" not in sys.argv: threading.Timer(.5, webbrowser.open, [url]).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        for j in JOBS.values(): j.kill()


if __name__ == "__main__":
    main()
