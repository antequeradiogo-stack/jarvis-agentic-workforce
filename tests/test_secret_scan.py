"""Sanity/secret scan: tracked and unignored files must contain no secrets, private data or runtime artifacts.

Gitignored runtime output (``var/``, ``*.jsonl``, ``.env``) is outside the scan. Untracked files that are not
ignored are still scanned, so a secret dropped in the working tree is still caught.
"""
import re, subprocess, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache"}
# patterns are split so this file does not match itself
PATTERNS = {
    "openai_key": r"s" r"k-[A-Za-z0-9_-]{20,}",
    "aws_access_key": r"AK" r"IA[0-9A-Z]{16}",
    "github_token": r"gh" r"[pousr]_[A-Za-z0-9]{30,}",
    "private_key": r"-----BEGIN [A-Z ]*PRIV" r"ATE KEY-----",
    "slack_token": r"xox" r"[baprs]-[A-Za-z0-9-]{10,}",
    "bearer_jwt": r"eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}",
    "hmac_gateway_token": r"gw1\.[A-Za-z0-9_-]{20,}\.",
    "assignment_secret": r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"\s]{12,}['\"]",
    "phone_intl": r"\+(47|351)\s?\d{2,3}\s?\d{2,3}\s?\d{2,4}",
    "iban": r"\b(NO|PT)\d{2}\s?\d{4}\s?\d{4}",
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net"}
FORBIDDEN_FILES = {".env", "id_rsa", "id_ed25519", "credentials.json", "used_nonces.txt", "effects.jsonl", "events.jsonl"}

BINARY_MEDIA = {".gif", ".png", ".jpg", ".jpeg", ".webp"}  # random bytes cause false text matches; checked separately


def repo_paths():
    """Tracked files plus untracked files that are not gitignored."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, check=True, capture_output=True)
    for rel in listed.stdout.split(b"\0"):
        if not rel:
            continue
        rel_s = rel.decode("utf-8", "surrogateescape")
        if set(Path(rel_s).parts) & SKIP_DIRS:
            continue
        p = ROOT / rel_s
        if p.is_file():
            yield p


def all_files():
    yield from repo_paths()


def files():
    return (p for p in all_files() if p.suffix.lower() not in BINARY_MEDIA)

class SecretScan(unittest.TestCase):
    def test_tree_not_empty(self):
        self.assertGreater(len(list(files())), 5)

    def test_no_secret_patterns(self):
        hits = []
        for p in files():
            text = p.read_text(errors="ignore")
            for name, pat in PATTERNS.items():
                if re.search(pat, text): hits.append(f"{p.relative_to(ROOT)}: {name}")
        self.assertEqual(hits, [])

    def test_only_example_emails(self):
        hits = [f"{p.relative_to(ROOT)}: {m.group(0)}" for p in files()
                for m in EMAIL.finditer(p.read_text(errors="ignore"))
                if m.group(1).lower() not in ALLOWED_EMAIL_DOMAINS]
        self.assertEqual(hits, [])

    def test_no_operational_files(self):
        bad = [str(p.relative_to(ROOT)) for p in files() if p.name in FORBIDDEN_FILES or p.suffix in {".pem", ".key", ".jsonl", ".log"}]
        self.assertEqual(bad, [])

    def test_media_files_carry_no_paths_or_metadata_text(self):
        bad = []
        for p in all_files():
            if p.suffix.lower() in BINARY_MEDIA:
                data = p.read_bytes()
                for needle in (b"/home/", b"/tmp/", b"/Users/", str(ROOT).encode(), b"tEXt", b"iTXt", b"eXIf"):
                    if needle in data: bad.append(f"{p.relative_to(ROOT)}: {needle!r}")
        self.assertEqual(bad, [])

    def test_scanner_detects_planted_secret(self):
        planted = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
        self.assertTrue(re.search(PATTERNS["aws_access_key"], planted))

    def test_scan_ignores_gitignored_runtime_output(self):
        planted = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
        runtime = ROOT / "var" / "qa-scan"
        effects = runtime / "mock_outbox" / "effects.jsonl"
        events = runtime / "events.jsonl"
        nonces = runtime / "used_nonces.txt"
        probe = ROOT / "qa_untracked_probe.txt"
        effects.parent.mkdir(parents=True, exist_ok=True)
        try:
            effects.write_text(planted + "\n")
            events.write_text("person@" + "gmail.com\n")
            nonces.write_text("nonce\n")
            probe.write_text(planted + "\n")
            scanned = list(files())
            scanned_set = {p.resolve() for p in scanned}
            for skipped in (effects, events, nonces):
                self.assertNotIn(skipped.resolve(), scanned_set)
            self.assertIn(probe.resolve(), scanned_set)
            self.assertIn((ROOT / "README.md").resolve(), scanned_set)
            secret_hits, email_hits = [], []
            for p in scanned:
                text = p.read_text(errors="ignore")
                for name, pat in PATTERNS.items():
                    if re.search(pat, text):
                        secret_hits.append(f"{p.relative_to(ROOT)}: {name}")
                email_hits += [f"{p.relative_to(ROOT)}: {m.group(0)}" for m in EMAIL.finditer(text)
                               if m.group(1).lower() not in ALLOWED_EMAIL_DOMAINS]
            self.assertEqual(secret_hits, [f"{probe.relative_to(ROOT)}: aws_access_key"])
            self.assertEqual(email_hits, [])
        finally:
            for p in (effects, events, nonces, probe):
                if p.exists():
                    p.unlink()
            for d in (effects.parent, runtime, ROOT / "var"):
                if d.is_dir():
                    try:
                        d.rmdir()
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()
