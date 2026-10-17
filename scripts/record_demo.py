#!/usr/bin/env python3
"""Record the README demo: runs the REAL CLI commands in a throwaway directory, sanitises the output (no absolute
paths, usernames or hostnames), and renders a terminal animation to docs/media/demo.gif (+ demo.cast, asciicast v2,
and a still frame demo_frame.png). Needs Pillow and a monospace TTF (DejaVu Sans Mono by default).

  python3 scripts/record_demo.py            # writes docs/media/demo.{gif,cast} and demo_frame.png

The approval key is a fresh random key per recording (never stored or shown)."""
from __future__ import annotations
import getpass, json, os, re, secrets, socket, subprocess, sys, tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "media"
FONT = os.environ.get("DEMO_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
BOLD = FONT.replace("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf")
COLS, ROWS, FS, PAD = 104, 30, 15, 14
BG, FG = (13, 17, 23), (201, 209, 217)
COLORS = {"prompt": (63, 185, 80), "cmd": (230, 237, 243), "comment": (139, 148, 158), "deny": (248, 81, 73),
          "allow": (63, 185, 80), "wait": (210, 153, 34), "crash": (219, 97, 162), "done": (88, 166, 255),
          "dim": (110, 118, 129)}
PROMPT = "jarvis-demo $ "

SCRIPT = [  # (comment, command or None, pause after output in seconds)
    ("# 1) Task: research a topic, draft a report, then EMAIL it. send_email is default-deny.",
     "python -m jarvis_workforce.run --workdir var --task examples/tasks/research_and_email.json --no-json", 7.0),
    ("# 2) A human reviews the EXACT request (the email body is the draft) ...",
     "python -m jarvis_workforce.approve --workdir var --run demo-email-001", 8.0),
    ("# ... and approves it: HMAC-signed, single-use, expires in 10 min.",
     "python -m jarvis_workforce.approve --workdir var --run demo-email-001 --yes", 4.5),
    ("# 3) Resume the same run: finished steps come from the checkpoint, the approved email goes out (mock).",
     "python -m jarvis_workforce.run --workdir var --task examples/tasks/research_and_email.json --no-json", 7.5),
    ("# 4) Crash AFTER the email is sent but BEFORE it is logged. Resuming must not send it twice.",
     "python -m jarvis_workforce.demo --scenario crash", 9.0),
]


def sanitise(text: str, tmp: str) -> str:
    for s in sorted({tmp, os.path.realpath(tmp), str(ROOT), str(Path.home())}, key=len, reverse=True):
        text = text.replace(s, ".")
    for s in {getpass.getuser(), socket.gethostname()}:
        if len(s) > 2:
            text = re.sub(rf"\b{re.escape(s)}\b", "user", text)
    return text


def capture() -> list[tuple[str, str, float, str]]:
    steps = []
    with tempfile.TemporaryDirectory(prefix="jw-rec-") as tmp:
        os.symlink(ROOT / "examples", Path(tmp) / "examples")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(ROOT / "src"), "LANG": "C.UTF-8",
               "JARVIS_APPROVAL_KEY": "hex:" + secrets.token_hex(32), "HOME": tmp}
        for comment, cmd, pause in SCRIPT:
            p = subprocess.run([sys.executable, *cmd.split()[1:]], cwd=tmp, env=env, capture_output=True, text=True, timeout=120)
            out = (p.stdout + p.stderr).replace("approval key source: JARVIS_APPROVAL_KEY", "approval key source: JARVIS_APPROVAL_KEY (env)")
            if p.returncode not in (0, 10):
                raise SystemExit(f"command failed ({p.returncode}): {cmd}\n{out}")
            steps.append((comment, cmd, pause, sanitise(out, tmp)))
    return steps


def wrap(line: str) -> list[str]:
    return [line[i:i + COLS] for i in range(0, max(len(line), 1), COLS)] or [""]


def style(line: str) -> str:
    s = line.lstrip()
    if line.startswith(PROMPT): return "prompt"
    if s.startswith("#"): return "comment"
    if "DENY" in s or s.startswith("x "): return "deny"
    if "GATE" in s and "ALLOW" in s or s.startswith("ok mock") or s.startswith("approved:"): return "allow"
    if s.startswith("||") or "approval required" in s: return "wait"
    if s.startswith("!!"): return "crash"
    if s.startswith("<<") or s.startswith(">>") or s.startswith("mock outbox"): return "done"
    if "ALLOW" in s or s.startswith("ok '") or "skipped" in s: return "dim"
    return "fg"


class Term:
    def __init__(self):
        self.lines: list[str] = []
        self.styles: list[str] = []
        self.font = ImageFont.truetype(FONT, FS)
        self.bold = ImageFont.truetype(BOLD, FS) if Path(BOLD).exists() else self.font
        l, t, r, b = self.font.getbbox("M" * 10)
        self.cw, self.ch = (r - l) / 10, int(FS * 1.35)
        self.size = (int(COLS * self.cw) + 2 * PAD, ROWS * self.ch + 2 * PAD + 26)

    def write_line(self, line: str):
        parts = wrap(line)
        self.lines.extend(parts)
        self.styles.extend([style(line)] * len(parts))

    def type_into_last(self, text: str):
        self.lines[-1] += text
        if len(self.lines[-1]) > COLS:
            extra = self.lines[-1][COLS:]; self.lines[-1] = self.lines[-1][:COLS]
            self.lines.append(extra); self.styles.append(self.styles[-1])

    def render(self, caret=True) -> Image.Image:
        im = Image.new("RGB", self.size, BG)
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, self.size[0], 26], fill=(22, 27, 34))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([PAD + i * 20, 8, PAD + i * 20 + 11, 19], fill=c)
        d.text((self.size[0] // 2, 13), "jarvis-agentic-workforce - offline demo (mock agents, synthetic data)",
               fill=COLORS["comment"], font=self.font, anchor="mm")
        vis, vst = self.lines[-ROWS:], self.styles[-ROWS:]
        for r, line in enumerate(vis):
            y = PAD + 26 + r * self.ch
            st = vst[r] if not line.startswith(PROMPT) else "prompt"
            if st == "prompt" and len(line) > len(PROMPT) and line[len(PROMPT):].lstrip().startswith("#"):
                d.text((PAD, y), PROMPT, fill=COLORS["prompt"], font=self.bold)
                d.text((PAD + len(PROMPT) * self.cw, y), line[len(PROMPT):], fill=COLORS["comment"], font=self.font)
                continue
            if st == "prompt":
                d.text((PAD, y), PROMPT, fill=COLORS["prompt"], font=self.bold)
                d.text((PAD + len(PROMPT) * self.cw, y), line[len(PROMPT):], fill=COLORS["cmd"], font=self.bold)
            else:
                d.text((PAD, y), line, fill=COLORS.get(st, FG), font=self.bold if st in ("done", "crash") else self.font)
        if caret and vis:
            r = len(vis) - 1
            x = PAD + len(vis[-1]) * self.cw
            d.rectangle([x, PAD + 26 + r * self.ch + 2, x + self.cw - 1, PAD + 26 + (r + 1) * self.ch - 2], fill=FG)
        return im


def build(steps):
    term, frames, cast, t = Term(), [], [], 0.0

    def snap(dur, caret=True):
        nonlocal t
        frames.append((term.render(caret), int(round(dur * 1000))))
        t += dur

    def emit(text):
        cast.append([round(t, 3), "o", text])

    term.write_line(PROMPT); emit(PROMPT); snap(1.2)
    for comment, cmd, pause, out in steps:
        for chunk_src, speed in ((comment, 0.022), (None, 0), (cmd, 0.035)):
            if chunk_src is None:  # newline + new prompt for the command
                term.write_line(PROMPT); emit("\r\n" + PROMPT); snap(0.35); continue
            for i in range(0, len(chunk_src), 3):
                piece = chunk_src[i:i + 3]
                term.type_into_last(piece); emit(piece); snap(speed * 3)
            snap(0.4)
        emit("\r\n")
        lines = out.rstrip("\n").split("\n")
        for i in range(0, len(lines), 2):  # output appears quickly, two lines per frame
            for ln in lines[i:i + 2]:
                term.write_line(ln); emit(ln + "\r\n")
            snap(0.06, caret=False)
        term.write_line(PROMPT); emit(PROMPT); snap(pause)
    snap(2.0)
    return frames, cast, term


def main() -> int:
    steps = capture()
    frames, cast, term = build(steps)
    joined = "\n".join(o for *_, o in steps)
    for leak in ("/home/", "/tmp/", "/Users/", str(ROOT), str(Path.home()), "jw-rec-"):
        if leak in joined:
            raise SystemExit(f"refusing to render: output still contains {leak!r}")
    OUT.mkdir(parents=True, exist_ok=True)
    # one fixed palette for all frames (no per-frame flicker, small file)
    pal = max((f for f, _ in frames), key=lambda im: len(im.getcolors(1 << 20) or [])).quantize(48, dither=Image.Dither.NONE)
    pframes = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f, _ in frames]
    pframes[0].save(OUT / "demo.gif", save_all=True, append_images=pframes[1:], duration=[d for _, d in frames],
                    loop=0, optimize=True, disposal=1)
    # still frame: the moment the crash-recovery result is on screen
    idx = max(i for i, (f, d) in enumerate(frames) if d >= 9000)
    frames[idx][0].save(OUT / "demo_frame.png", optimize=True)
    header = {"version": 2, "width": COLS, "height": ROWS, "title": "jarvis-agentic-workforce demo",
              "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"}}
    (OUT / "demo.cast").write_text("\n".join([json.dumps(header)] + [json.dumps(e, ensure_ascii=False) for e in cast]) + "\n")
    total = sum(d for _, d in frames) / 1000
    print(f"frames={len(frames)} duration={total:.1f}s size={term.size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
