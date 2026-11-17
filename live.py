"""Live auto-clipper: records a YouTube live stream, watches chat speed, and when chat suddenly
spikes (something just happened) cuts the last ~60s and renders a captioned vertical clip.

usage: python live.py LIVE_URL [--before 60] [--after 10] [--ratio 3] [--model base]
Stop with Ctrl+C. Clips land in ./clips as live_HHMMSS.mp4.
"""
import argparse, re, signal, subprocess, sys, threading, time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import clip

SEGS = clip.SRC / "live"  # main() narrows this to one folder per stream
SEG = 5  # seconds per recorded chunk


class Spike:
    """Chat-speed spike detector. feed(t, n) every tick; True when the last `window` seconds run
    `ratio`x faster than the `history` before them (after warmup, outside cooldown)."""

    def __init__(self, window=10, history=180, ratio=3.0, min_rate=0.5, cooldown=60):
        self.w, self.h, self.r, self.min, self.cool = window, history, ratio, min_rate, cooldown
        self.ev, self.start, self.last = deque(), None, -1e9

    def feed(self, t, n):
        self.start = t if self.start is None else self.start
        self.ev.append((t, n))
        while self.ev and self.ev[0][0] < t - self.h - self.w: self.ev.popleft()
        now = sum(c for s, c in self.ev if s > t - self.w) / self.w
        span = min(self.h, t - self.start - self.w)
        if span < 30: return False  # warmup: need a baseline first
        base = sum(c for s, c in self.ev if s <= t - self.w) / span
        self.rate, self.base = now, base
        if now >= self.min and now >= self.r * max(base, .1) and t - self.last > self.cool:
            self.last = t
            return True
        return False


def record(url, stop):
    """ffmpeg records the live video+audio into 5s files named by the wall-clock second they
    start, so any moment can be found later. yt-dlp supplies the stream URLs; they expire after
    a few hours, so on any ffmpeg exit we fetch fresh ones and carry on."""
    SEGS.mkdir(parents=True, exist_ok=True)
    while not stop.is_set():
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-g",
                            "-f", "bv*[height<=1080]+ba/b", url], capture_output=True, text=True)
        urls = r.stdout.split()
        if not urls:
            print("\ncan't get stream (ended or offline):", r.stderr.strip()[-200:], flush=True); return
        inputs = [x for u in urls for x in ("-i", u)]
        maps = ["-map", "0:v", "-map", "1:a"] if len(urls) == 2 else []
        recorder = subprocess.Popen([clip.FFMPEG, "-loglevel", "error", *inputs, *maps, "-c", "copy",
                                     "-f", "segment", "-segment_time", str(SEG), "-reset_timestamps", "1",
                                     "-strftime", "1", str(SEGS / "%Y%m%d%H%M%S.ts")])  # %s breaks on Windows
        while recorder.poll() is None and not stop.is_set(): time.sleep(1)
        recorder.kill()
        time.sleep(3)  # don't hammer YouTube if something keeps failing


def segments():
    """Recorded chunks as [(start_epoch, path)], oldest first."""
    return sorted((time.mktime(time.strptime(f.stem, "%Y%m%d%H%M%S")), f)
                  for f in SEGS.glob("*.ts") if f.stem.isdigit())


def start_chat(url):
    """yt-dlp streams live chat into a growing file, one JSON line per chat action."""
    for f in SEGS.glob("chat.*"): f.unlink()
    p = subprocess.Popen([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--skip-download",
                          "--write-subs", "--sub-langs", "live_chat", "-o", str(SEGS / "chat"), url])
    return p, SEGS / "chat.live_chat.json.part"


def make_clip(t_spike, idx, a):
    """Join the chunks around the spike, transcribe just that, render with captions + tracking."""
    lo, hi = t_spike - a.before, t_spike + a.after
    use = [(s, f) for s, f in segments() if s + SEG > lo and s < hi]
    if not use:
        print("  !! no recorded video for that moment", flush=True); return
    name = time.strftime("live_%H%M%S", time.localtime(t_spike))
    joined = clip.SRC / f"{name}.ts"
    (SEGS / f"{name}.txt").write_text("".join(f"file '{f.name}'\n" for _, f in use))
    subprocess.run([clip.FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-i", f"{name}.txt",
                    "-c", "copy", str(joined)], cwd=SEGS, check=True)
    try:
        words = clip.transcribe(joined, a.model)
        start = max(0, lo - use[0][0])
        clip.render(joined, {"start": start, "end": hi - use[0][0], "title": name}, idx, words, a)
    finally:
        joined.unlink(missing_ok=True); (SEGS / f"{name}.txt").unlink(missing_ok=True)


def _interrupt(*_):
    raise KeyboardInterrupt  # SIGTERM / Ctrl+Break (the web UI's Stop) -> same clean shutdown as Ctrl+C


def main():
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("--before", type=float, default=60, help="seconds of video before the spike")
    p.add_argument("--after", type=float, default=10, help="seconds after the spike (the reaction)")
    p.add_argument("--ratio", type=float, default=3, help="spike = chat this many x faster than usual")
    p.add_argument("--window", type=float, default=10, help="seconds of chat that count as 'now'")
    p.add_argument("--history", type=float, default=180, help="seconds of chat that count as 'usual'")
    p.add_argument("--cooldown", type=float, default=60, help="min seconds between clips")
    p.add_argument("--model", default="base", help="whisper size; base is fast, small more accurate")
    p.add_argument("--font", default="Arial Black")
    p.add_argument("--color", default="FFE600,00FF66,00E5FF,FF3EA5")
    p.add_argument("--words", type=int, default=3)
    p.add_argument("--no-crop", action="store_true")
    a = p.parse_args()
    if not a.url.startswith(("http://", "https://")): raise SystemExit("need a live stream URL")
    for s in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, s): signal.signal(getattr(signal, s), _interrupt)

    global SEGS
    SEGS = SEGS / re.sub(r"\W+", "_", a.url.split("//", 1)[1])[-60:]  # one folder per stream
    stop = threading.Event()
    rec = threading.Thread(target=record, args=(a.url, stop), daemon=True)
    rec.start()
    SEGS.mkdir(parents=True, exist_ok=True)
    chat_proc, chat_file = start_chat(a.url)
    spike = Spike(a.window, a.history, a.ratio, cooldown=a.cooldown)
    worker = ThreadPoolExecutor(1)  # ponytail: one clip at a time; whisper on CPU is the bottleneck
    pos, idx, tick = 0, 0, 0
    print("recording + watching chat... (Ctrl+C to stop)", flush=True)
    try:
        while True:
            time.sleep(1)
            tick += 1
            n = 0
            if chat_file.exists():
                with open(chat_file, "rb") as f:
                    f.seek(pos); data = f.read()
                done = data.rfind(b"\n") + 1  # count only complete lines
                # first chunk is old chat history, not live speed: skip it
                n, pos = (data[:done].count(b"\n") if pos else 0), pos + done
            now = time.time()
            if spike.feed(now, n):
                idx += 1
                print(f"\n>> SPIKE {time.strftime('%H:%M:%S')}: chat {spike.rate:.1f}/s vs usual "
                      f"{spike.base:.1f}/s -> clipping last {a.before:.0f}s (ready in a minute or two)",
                      flush=True)
                worker.submit(lambda t=now, i=idx: time.sleep(a.after) or make_clip(t, i, a)) \
                      .add_done_callback(lambda f: f.exception() and print("  !! clip failed:", f.exception(), flush=True))
            if tick % 5 == 0:
                if hasattr(spike, "rate"):
                    print(f"\rchat {spike.rate:.1f}/s  usual {spike.base:.1f}/s  clips {idx}   ", end="", flush=True)
                else:
                    print("\rlearning how fast chat usually is...", end="", flush=True)
                keep = now - a.before - a.after - 3 * a.cooldown  # disk: drop chunks nobody will need
                for s, f in segments():
                    if s < keep: f.unlink(missing_ok=True)
            if not rec.is_alive():
                print("\nrecording stopped", flush=True); break
    except KeyboardInterrupt:
        print("\nstopping... (finishing any clip in progress)", flush=True)
    finally:
        stop.set(); chat_proc.kill()
        worker.shutdown(wait=True)


if __name__ == "__main__":
    main()
