"""Checks for the web UI server: input validation, path safety, local-only access, video seeking.
Run: python test_ui.py"""
import http.client, json, threading
from http.server import ThreadingHTTPServer

import ui

# --- form fields -> argv: valid input passes, bad input is refused
argv = ui.build_argv("video", {"url": "https://youtu.be/x", "clips": "3", "min": "20", "max": "60",
                                "font": "Arial Black", "color": "FFE600,00FF66", "words": "3", "no_crop": True})
assert argv[3] == "https://youtu.be/x" and "--no-crop" in argv and argv[argv.index("--clips") + 1] == "3"
for kind, bad in [("video", {"url": "--exec=calc"}),                       # flag injection via url
                  ("video", {"url": "https://a", "font": "Arial,1,2"}),    # comma breaks caption style line
                  ("video", {"url": "https://a", "color": "red"}),
                  ("video", {"url": "https://a", "clips": "999"}),
                  ("video", {"url": "https://a", "min": "90", "max": "30"}),
                  ("live", {"url": "https://a", "ratio": "0.5"}),
                  ("nope", {"url": "https://a"})]:
    try: ui.build_argv(kind, bad); raise AssertionError(f"should reject {bad}")
    except ValueError: pass
assert "--clips" not in ui.build_argv("live", {"url": "https://a", "clips": "3"}), "live ignores video-only fields"

# --- only plain .mp4 names inside clips/
ui.OUT.mkdir(exist_ok=True)
probe = ui.OUT / "zz_test_probe.mp4"
probe.write_bytes(bytes(range(256)) * 4)  # 1024 bytes
try:
    assert ui.clip_path("zz_test_probe.mp4") == probe
    for bad in ["../clip.py", "..\\ui.py", "src/x.mp4", ".hidden.mp4", "zz_test_probe.txt", "missing.mp4"]:
        assert ui.clip_path(bad) is None, bad

    # --- live server checks
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def req(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body, {"Host": f"127.0.0.1:{port}", **(headers or {})})
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, dict(r.getheaders()), data

    s, _, page = req("GET", "/")
    assert s == 200 and b"<title>Clipper</title>" in page
    s, _, st = req("GET", "/api/state")
    assert s == 200 and any(c["name"] == probe.name for c in json.loads(st)["clips"])
    assert req("GET", "/api/state", headers={"Host": "evil.example"})[0] == 403, "DNS-rebinding guard"
    assert req("POST", "/api/jobs", "kind=video")[0] == 415, "non-JSON POST (cross-site form) refused"
    s, _, e = req("POST", "/api/jobs", json.dumps({"kind": "video", "url": "nope"}), {"Content-Type": "application/json"})
    assert s == 400 and b"https://" in e
    s, h, part = req("GET", f"/clips/{probe.name}", headers={"Range": "bytes=100-199"})
    assert s == 206 and part == probe.read_bytes()[100:200] and h["Content-Range"] == "bytes 100-199/1024"
    s, _, tail = req("GET", f"/clips/{probe.name}", headers={"Range": "bytes=-24"})
    assert s == 206 and tail == probe.read_bytes()[-24:]
    assert req("GET", "/clips/..%2Fclip.py")[0] == 404
    assert req("DELETE", f"/api/clips/{probe.name}")[0] == 415, "delete needs JSON"
    assert req("DELETE", f"/api/clips/{probe.name}", "{}", {"Content-Type": "application/json"})[0] == 200
    assert not probe.exists()
    srv.shutdown()
finally:
    probe.unlink(missing_ok=True)
print("ok")
