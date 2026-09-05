# -*- coding: utf-8 -*-
"""Agent 侧 Channel 客户端：从 env CHANNEL_SOCKET 读 socket，发一帧 JSON，等应答。"""
import os, sys, json, socket

def send_frame(typ, value):
    sock = os.environ.get("CHANNEL_SOCKET")
    if not sock:
        print("错误：未设置 CHANNEL_SOCKET（本进程非 Channel 任务上下文）", file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(sock):
        print(f"错误：Channel socket 不存在 {sock}", file=sys.stderr)
        sys.exit(2)
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(sock)
        frame = json.dumps({"type": typ, **({"text": value} if typ == "reply" else {"path": value})}, ensure_ascii=False)
        s.sendall((frame + "\n").encode("utf-8"))
        # 读应答
        data = b""
        while b"\n" not in data:
            c = s.recv(4096)
            if not c: break
            data += c
        line = data.split(b"\n", 1)[0].decode("utf-8", "ignore").strip()
        resp = json.loads(line) if line else {}
        s.close()
        if resp.get("ok"):
            print("已发送", flush=True)
            sys.exit(0)
        print(f"错误：{resp.get('error', '未知')}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
