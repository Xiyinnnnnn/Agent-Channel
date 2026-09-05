#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent-Channel 最小示例 Agent（零依赖，纯协议演示）

演示如何被 Channel Manager 驱动：
  1. Channel 以子进程方式拉起本程序，stdin 保持长开；
  2. 每条 IM 消息 = 一行单物理行任务，写进 stdin（input() 一次读一行）；
  3. Agent 处理完后，通过 Unix Socket(env CHANNEL_SOCKET) 发回一帧 JSON
     即完成「主动回复」，无需打印 stdout（stdout 仅作会话日志）。

用法（由 Channel 自动 spawn，无需手动运行）：
  channel/config.json -> "agent": "./agents/demo_agent.py"

在真实项目里，把本程序换成任意满足同一协议的大模型 Agent 即可。
"""
import os, sys, json, socket

def channel_send(typ, value):
    """向 Channel Manager 发一帧。typ: reply=文本回复 / file=发送文件。"""
    sock = os.environ.get("CHANNEL_SOCKET")
    if not sock or not os.path.exists(sock):
        return
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(sock)
        frame = {"type": typ}
        if typ == "reply":
            frame["text"] = value
        else:
            frame["path"] = value
        s.sendall((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
        s.recv(4096)   # 读应答 {"ok":true,...}
        s.close()
    except Exception:
        pass

def main():
    print("[demo-agent] ready: 等待任务 (stdin 单行)", flush=True)
    for line in sys.stdin:
        task = line.strip()
        if not task:
            continue
        if task == "/new":
            continue   # /new 由 Manager 处理，这里不出现
        # 演示：原样回显任务内容（可把这里换成任意真实 Agent 逻辑）
        channel_send("reply", "demo-agent 已收到任务：\n" + task[:500])
    print("[demo-agent] stdin EOF，退出", flush=True)

if __name__ == "__main__":
    main()
