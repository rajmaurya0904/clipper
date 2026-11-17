"""YouTube video (or local file) -> vertical viral clips with word-by-word captions.

usage: python clip.py URL_OR_FILE [--clips 3] [--min 20] [--max 60] [--model small]
                                  [--font "Arial Black"] [--color FFE600,00FF66] [--words 3] [--no-crop]
Set ANTHROPIC_API_KEY to let Claude pick the viral moments; otherwise a heuristic picks them.
"""
import argparse, json, os, re, shutil, subprocess
from collections import Counter
from pathlib import Path

import imageio_ffmpeg
import yt_dlp

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "clips"           # finished clips
SRC = OUT / "src"              # downloads, transcripts, live recordings
BIN = ROOT / "_ffbin"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
# whisper shells out to plain "ffmpeg"; expose the bundled one under that name
BIN.mkdir(exist_ok=True)
_ff = BIN / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
if not _ff.exists():
    shutil.copy(FFMPEG, _ff); _ff.chmod(0o755)
os.environ["PATH"] = str(BIN) + os.pathsep + os.environ["PATH"]


def download(url):
    SRC.mkdir(parents=True, exist_ok=True)
    # SDR only: HDR streams render grey/washed-out without tone mapping
    opts = {"format": "bv*[height<=1080][dynamic_range=SDR]+ba/b[dynamic_range=SDR]/b",
            "outtmpl": str(SRC / "%(id)s.%(ext)s"), "merge_output_format": "mp4",
            "ffmpeg_location": FFMPEG, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url)
    return SRC / f"{info['id']}.mp4", info.get("title", "")


def transcribe(video, model):
    print(f"transcribing ({model})... this is the slow part", flush=True)
    import whisper  # ponytail: openai-whisper, since faster-whisper's DLL is blocked by Windows App Control here
    r = whisper.load_model(model).transcribe(str(video), word_timestamps=True, fp16=False)
    return [{"w": w["word"].strip(), "s": w["start"], "e": w["end"]}
            for s in r["segments"] for w in s.get("words", [])]


def sentences(words):
    out, cur = [], []
    for w in words:
        cur.append(w)
        if re.search(r"[.!?]$", w["w"]):
            out.append(cur); cur = []
    if cur: out.append(cur)
    return [{"s": c[0]["s"], "e": c[-1]["e"], "t": " ".join(x["w"] for x in c)} for c in out]


def pick_claude(sents, title, n, lo, hi):
    import anthropic
    lines = "\n".join(f"[{s['s']:.1f}-{s['e']:.1f}] {s['t']}" for s in sents)
    msg = anthropic.Anthropic().messages.create(
        model="claude-sonnet-5-5", max_tokens=2000,
        messages=[{"role": "user", "content":
            f"Video title: {title}\nTranscript with timestamps (seconds):\n{lines}\n\n"
            f"Pick the {n} most viral, self-contained moments for TikTok/Shorts, each {lo}-{hi}s. "
            "Start on a strong hook, end on a complete thought. Reply with ONLY a JSON array: "
            '[{"start": float, "end": float, "title": "short catchy title"}]'}])
    text = msg.content[0].text
    return json.loads(text[text.index("["): text.rindex("]") + 1])


def pick_heuristic(sents, n, lo, hi):
    # ponytail: scores sentence-aligned windows by speech density + !/? punch; swap for Claude via API key
    cands = []
    for i, a in enumerate(sents):
        txt = ""
        for b in sents[i:]:
            dur = b["e"] - a["s"]
            if dur > hi: break
            txt += " " + b["t"]
            if dur >= lo:
                score = len(txt.split()) / dur + 2 * txt.count("!") + txt.count("?")
                cands.append((score, a["s"], b["e"]))
    picks = []
    for score, s, e in sorted(cands, reverse=True):
        if all(e <= p[0] or s >= p[1] for p in picks):
            picks.append((s, e))
        if len(picks) == n: break
    return [{"start": s, "end": e, "title": f"clip{i+1}"} for i, (s, e) in enumerate(sorted(picks))]


def ts(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def ass(words, start, end, path, font, colors, per_line):
    """Word-by-word captions: each line pops in, the spoken word bounces up in colour,
    big/loud words (7+ letters, '!', numbers) bounce harder. Colour rotates per line."""
    cols = [f"&H00{c[4:6]}{c[2:4]}{c[0:2]}&" for c in colors.split(",")]  # RRGGBB -> ASS &HBBGGRR
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, "
            "BorderStyle, Outline, Shadow, Alignment, MarginV\n"
            f"Style: D,{font},100,&H00FFFFFF,&H00000000,&H64000000,1,1,9,5,2,380\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Text\n")
    ws = [w for w in words if w["s"] >= start and w["e"] <= end]
    ev = []
    for g, i in enumerate(range(0, len(ws), per_line)):
        grp, hl = ws[i:i + per_line], cols[g % len(cols)]
        for j, cur in enumerate(grp):
            nxt = grp[j + 1]["s"] if j + 1 < len(grp) else cur["e"]
            parts = []
            for k, w in enumerate(grp):
                t = w["w"].upper().rstrip(".,")
                if k == j:
                    big = len(t.strip("!?")) >= 7 or "!" in t or any(c.isdigit() for c in t)
                    peak, rest = (150, 130) if big else (128, 115)
                    tag = (f"\\c{hl}\\fscx{rest * .7:.0f}\\fscy{rest * .7:.0f}"
                           f"\\t(0,70,\\fscx{peak}\\fscy{peak})\\t(70,160,\\fscx{rest}\\fscy{rest})")
                elif j == 0:  # whole line pops in on its first word
                    tag = "\\c&HFFFFFF&\\fscx70\\fscy70\\t(0,90,\\fscx100\\fscy100)"
                else:
                    tag = "\\c&HFFFFFF&\\fscx100\\fscy100"
                parts.append(f"{{{tag}}}{t}")
            ev.append(f"Dialogue: 0,{ts(cur['s'] - start)},{ts(nxt - start)},D,{{\\be1}}{' '.join(parts)}")
    path.write_text(head + "\n".join(ev), encoding="utf-8")


def bars(video, start, end):
    """ffmpeg cropdetect over the clip -> (w, h, x, y) of the picture without baked-in black bars."""
    r = subprocess.run([FFMPEG, "-ss", str(start), "-t", str(min(10, end - start)), "-i", str(video),
                        "-vf", "format=gray,cropdetect=limit=24:round=2:skip=0:reset=1",  # gray: 10-bit fools it
                        "-an", "-f", "null", "-"], capture_output=True, text=True)
    found = re.findall(r"crop=(\d+:\d+:\d+:\d+)", r.stderr)
    # most common per-frame result, so fades/dark scenes don't skew it
    return tuple(map(int, Counter(found).most_common(1)[0][0].split(":")))


YUNET = BIN / "yunet.onnx"
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")


DET_FPS, OUT_FPS = 8, 30


def smooth_path(xs, cuts, tau=.45, dead=.03):
    """xs: face centre 0-1 (or None) per detection sample; cuts: True where a new shot starts.
    -> camera position per OUT_FPS tick. Eases toward the face like a camera operator
    (ignores <3% wobble), snaps instantly on scene cuts so it never pans across a cut."""
    shot = [sum(cuts[:i + 1]) for i in range(len(xs))]
    fill = list(xs)
    for order in (range(len(xs)), reversed(range(len(xs)))):  # forward then backward fill, within a shot
        last = None
        for i in order:
            if last is not None and fill[i] is None and shot[last] == shot[i]: fill[i] = fill[last]
            if fill[i] is not None: last = i
    med = []
    for i in range(len(xs)):  # 5-sample median inside the shot kills detector flicker
        win = sorted(fill[k] for k in range(max(0, i - 2), min(len(xs), i + 3))
                     if shot[k] == shot[i] and fill[k] is not None)
        med.append(win[len(win) // 2] if win else None)
    k = 1 - 2.718281828 ** (-1 / (OUT_FPS * tau))
    out, pos, tgt, prev = [], None, None, -1
    for n in range(len(xs) * OUT_FPS // DET_FPS):
        i = n * DET_FPS // OUT_FPS
        m = med[i]
        if m is not None and (pos is None or (cuts[i] and i != prev)):
            pos = tgt = m  # first frame or new shot: hard cut
        elif m is not None and abs(m - tgt) > dead:
            tgt = m
        if pos is not None: pos += (tgt - pos) * k
        out.append(.5 if pos is None else pos)
        prev = i
    return out


def track(video, start, end, w, h, bx, by, cw):
    """Face-follow camera -> [(t, crop_x)] for a cw-wide 9:16 window over the de-barred frame."""
    import cv2, numpy as np
    if not YUNET.exists():
        import urllib.request; urllib.request.urlretrieve(YUNET_URL, YUNET)
    W, H = 480, round(480 * h / w / 2) * 2
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(start), "-t", str(end - start),
                          "-i", str(video), "-vf", f"crop={w}:{h}:{bx}:{by},fps={DET_FPS},scale={W}:{H}",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True).stdout
    det = cv2.FaceDetectorYN.create(str(YUNET), "", (W, H), 0.6)
    xs, cuts, prev = [], [], None
    for f in np.frombuffer(raw, np.uint8).reshape(-1, H, W, 3):
        faces = det.detect(f)[1]
        xs.append(None if faces is None else
                  max(faces, key=lambda r: r[2] * r[3])[[0, 2]] @ [1, .5] / W)  # biggest face centre
        tiny = cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (64, 36)).astype(int)
        cuts.append(prev is None or abs(tiny - prev).mean() > 12)  # scene cut; normal motion scores ~1-3, cuts 25+
        prev = tiny
    out, last = [], None
    for n, p in enumerate(smooth_path(xs, cuts)):
        x = int(min(max(p * w - cw / 2, 0), w - cw))
        if x != last: out.append((n / OUT_FPS, x)); last = x
    return out or [(0, (w - cw) // 2)]


def render(video, clip, idx, words, a):
    OUT.mkdir(exist_ok=True)
    name = re.sub(r"[^\w-]+", "_", clip["title"]).strip("_")[:40] or f"clip{idx}"
    out = f"{name}.mp4" if name == video.stem else f"{video.stem}_{idx:02d}_{name}.mp4"
    tmp = f".{video.stem}_{idx:02d}"  # per-clip temp names: parallel jobs don't collide
    sub = OUT / f"{tmp}.ass"
    ass(words, clip["start"], clip["end"], sub, a.font, a.color, a.words)
    w, h, bx, by = bars(video, clip["start"], clip["end"])
    vf = f"crop={w}:{h}:{bx}:{by},"
    if a.no_crop:
        vf += "pad=iw:iw*16/9:(ow-iw)/2:(oh-ih)/2,"
    else:
        cw = h * 9 // 16 // 2 * 2
        moves = track(video, clip["start"], clip["end"], w, h, bx, by, cw)
        cmds = OUT / f"{tmp}.cmd"
        cmds.write_text("\n".join(f"{t:.2f} crop@fc x {x};" for t, x in moves))
        vf += f"sendcmd=f={cmds.name},crop@fc={cw}:{h}:{moves[0][1]}:0,"
    vf += f"scale=1080:1920,ass={sub.name}"
    try:
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(clip["start"]),
                        "-to", str(clip["end"]), "-i", str(video.resolve()), "-vf", vf,
                        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "aac",
                        "-movflags", "+faststart", f"{tmp}.mp4"],
                       cwd=OUT, check=True)  # cwd=OUT: filter args use bare names, dodging Windows path escaping
        os.replace(OUT / f"{tmp}.mp4", OUT / out)  # only finished clips get a visible name
    finally:
        for ext in ("ass", "cmd", "mp4"): (OUT / f"{tmp}.{ext}").unlink(missing_ok=True)
    print("  ->", OUT / out, flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("--clips", type=int, default=3)
    p.add_argument("--min", type=float, default=20)
    p.add_argument("--max", type=float, default=60)
    p.add_argument("--model", default="small", help="whisper size: tiny/base/small/medium/large-v3")
    p.add_argument("--font", default="Arial Black")
    p.add_argument("--color", default="FFE600,00FF66,00E5FF,FF3EA5",
                   help="highlight colours RRGGBB, comma list rotates per line")
    p.add_argument("--words", type=int, default=3, help="words per caption line")
    p.add_argument("--no-crop", action="store_true", help="letterbox instead of crop")
    a = p.parse_args()

    if a.url.startswith(("http://", "https://")):
        print("downloading...", flush=True)
        video, title = download(a.url)
    else:
        video, title = Path(a.url), Path(a.url).stem
        if not video.is_file(): raise SystemExit(f"file not found: {video}")
    SRC.mkdir(parents=True, exist_ok=True)
    cache = SRC / f"{video.stem}.{a.model}.json"  # re-runs (new style/clip count) skip transcription
    if cache.exists():
        print("using saved transcript", flush=True)
        words = json.loads(cache.read_text())
    else:
        words = transcribe(video, a.model)
        cache.write_text(json.dumps(words))
    sents = sentences(words)
    clips = None
    if os.getenv("ANTHROPIC_API_KEY"):
        print("asking Claude for the best moments...", flush=True)
        try:
            clips = pick_claude(sents, title, a.clips, a.min, a.max)
        except Exception as e:  # bad key / no credit / odd reply: still make clips
            print(f"Claude failed ({e}); using built-in picker", flush=True)
    clips = clips or pick_heuristic(sents, a.clips, a.min, a.max)
    print(f"found {len(clips)} moments", flush=True)
    for i, c in enumerate(clips, 1):
        print(f"[{i}/{len(clips)}] {c['start']:.1f}-{c['end']:.1f}s {c['title']}", flush=True)
        render(video, c, i, words, a)
    print("done", flush=True)


if __name__ == "__main__":
    main()
