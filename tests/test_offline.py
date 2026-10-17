"""Offline runner: the socket guard blocks network in a child Python; under scripts/run_offline.sh the
current process is blocked too (and, when unshare works, there is no route at all)."""
import os, socket, subprocess, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "offline"
PROBE = ("import socket\ntry:\n    socket.create_connection(('192.0.2.1', 80), timeout=1)\n    print('CONNECTED')\n"
         "except OSError as e:\n    print('BLOCKED', e)\n")


class Guard(unittest.TestCase):
    def test_guard_blocks_child_python(self):
        env = dict(os.environ, PYTHONPATH=str(GUARD))
        out = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=30).stdout
        self.assertIn("network disabled by offline test guard", out)

    def test_guard_allows_unix_sockets(self):
        env = dict(os.environ, PYTHONPATH=str(GUARD))
        code = "import socket; a, b = socket.socketpair(); a.sendall(b'ok'); print(b.recv(2).decode())"
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30).stdout
        self.assertEqual(out.strip(), "ok")

    @unittest.skipUnless(os.environ.get("JARVIS_OFFLINE") == "1", "only under scripts/run_offline.sh")
    def test_current_process_is_offline(self):
        with self.assertRaises(OSError):
            socket.create_connection(("192.0.2.1", 80), timeout=1)
        with self.assertRaises(OSError):
            socket.getaddrinfo("example.com", 443)

    @unittest.skipUnless(os.environ.get("JARVIS_NETNS") == "1", "only when unshare -rn is available")
    def test_netns_blocks_raw_sockets_without_guard(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # no Python guard in the child
        out = subprocess.run([sys.executable, "-S", "-c", PROBE], env=env, capture_output=True, text=True, timeout=30).stdout
        self.assertIn("BLOCKED", out)
        self.assertNotIn("guard", out)


if __name__ == "__main__":
    unittest.main()
