"""inspect_flipbook.py — probe H21 SceneViewer.flipbook signature via socket."""
import inspect
import json
import socket
import struct
import sys


def send(sock, cmd_type, params):
    payload = json.dumps({"type": cmd_type, "params": params}).encode()
    sock.sendall(struct.pack(">I", len(payload)) + payload)
    buf = b""
    while len(buf) < 4:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    n = struct.unpack(">I", buf[:4])[0]
    buf = buf[4:]
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return json.loads(buf.decode())


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(60)
    sock.connect(("127.0.0.1", 9876))
    # Build probe code WITHOUT nested quotes
    code = (
        "import hou\n"
        "import inspect\n"
        "sv = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)\n"
        "print('sv has flipbook attr:', hasattr(sv, 'flipbook'))\n"
        "try:\n"
        "    sig = inspect.signature(sv.flipbook)\n"
        "    print('sv.flipbook sig:', sig)\n"
        "except Exception as e:\n"
        "    print('sv.flipbook sig err:', e)\n"
        "vp = sv.curViewport()\n"
        "print('vp type:', type(vp).__name__)\n"
        "print('vp has flipbook attr:', hasattr(vp, 'flipbook'))\n"
        "try:\n"
        "    sig2 = inspect.signature(vp.flipbook)\n"
        "    print('vp.flipbook sig:', sig2)\n"
        "except Exception as e:\n"
        "    print('vp.flipbook sig err:', e)\n"
    )
    resp = send(sock, "execute_code", {
        "code": code,
        "policy": "normal",
        "capture_diff": False,
    })
    print(json.dumps(resp, ensure_ascii=False, indent=2)[:2500])
    sock.close()


if __name__ == "__main__":
    main()