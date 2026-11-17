from clip import smooth_path, OUT_FPS, DET_FPS

# face jumps .2 -> .8 mid-shot: camera eases (no jump > 5% per tick) and arrives
p = smooth_path([.2] * 8 + [.8] * 24, [True] + [False] * 31)
assert max(abs(a - b) for a, b in zip(p, p[1:])) < .05, "pan should be smooth"
assert abs(p[-1] - .8) < .02, "camera should reach the face"
# same jump at a scene cut: snaps immediately
p = smooth_path([.2] * 8 + [.8] * 8, [True] + [False] * 7 + [True] + [False] * 7)
assert abs(p[8 * OUT_FPS // DET_FPS + 1] - .8) < .01, "should hard-cut on new shot"
# missing faces + tiny wobble: holds still
p = smooth_path([.5, None, .51, None, .49, .5, None, .5], [True] + [False] * 7)
assert max(p) - min(p) < .001, "wobble/gaps should not move camera"

from live import Spike
s = Spike(window=10, history=60, ratio=3, cooldown=60)
fired = [t for t in range(200) if s.feed(t, 8 if 100 <= t < 112 else 1)]  # 1 msg/s, burst at 100s
assert fired and 100 <= fired[0] <= 106, f"should fire early in the burst, got {fired}"
assert len(fired) == 1, "cooldown should stop repeat clips for the same moment"
s = Spike(window=10, history=60, ratio=3)
assert not any(s.feed(t, 2) for t in range(200)), "steady chat must not fire"
print("ok")
