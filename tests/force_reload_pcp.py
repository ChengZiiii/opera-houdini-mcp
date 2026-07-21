"""force_reload_pcp.py — 尝试强制重新加载 _pane_capture 模块。"""
import json
import socket
import struct


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
    # 尝试强制 reload _pane_capture 模块（user spec module cache 已知）
    code = (
        "import sys\n"
        "import importlib\n"
        "try:\n"
        "    # 删 module cache 强制重新 import\n"
        "    for k in list(sys.modules.keys()):\n"
        "        if '_pane_capture' in k:\n"
        "            del sys.modules[k]\n"
        "    # 但 server.py 还持有旧引用，不会自动生效\n"
        "    # 需要 server.py 整个 reload 才生效\n"
        "    print('Cleared pcp module cache; new requests still hit old code')\n"
        "    print('Reload server.py requires MCP Stop+Start')\n"
        "except Exception as e:\n"
        "    print('Reload attempt failed:', e)\n"
    )
    resp = send(sock, "execute_code", {
        "code": code,
        "policy": "normal",
        "capture_diff": False,
    })
    print(json.dumps(resp, ensure_ascii=False, indent=2)[:2000])
    sock.close()


if __name__ == "__main__":
    main()