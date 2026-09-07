# -*- coding: utf-8 -*-
"""context_token 持久化存储（task 书 16-19 节）。

- 每次 inbound 收到新消息 → 按 conversation_id 保存最新 context_token
- 按 conversation/peer 隔离，禁止所有用户共用一个 token
- 原子写入、重启可恢复
- 不伪造 refresh API；过期自然等待下次 inbound 更新
存储: <state_dir>/wechat-context.json  (与登录态同一 HOME 持久目录)
"""
import json
import os
import tempfile

# 与 accounts 同根目录（accounts 会再拼 /ilink-weixin/accounts）
_DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "agent-terminal", "channel")

_lock = __import__("threading").RLock()


def _path(state_dir=None):
    return os.path.join(state_dir or _DEFAULT_DIR, "wechat-context.json")


def load(state_dir=None):
    """读回 {conversation_id: {"context_token":..., "updated_at":...}}；无则 {}。"""
    p = _path(state_dir)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def get(conversation_id, state_dir=None):
    return (load(state_dir).get(conversation_id) or {}).get("context_token")


def save(conversation_id, context_token, state_dir=None):
    """原子写入单条。conversation_id 建议 platform 前缀化（如 private:o9cq…）避免跨平台碰撞。"""
    if not conversation_id or not context_token:
        return
    import time
    with _lock:
        data = load(state_dir)
        data[conversation_id] = {
            "context_token": str(context_token),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        p = _path(state_dir)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".wechat-context-", dir=os.path.dirname(p))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
            try:
                os.chmod(p, 0o600)
            except Exception:
                pass
        finally:
            if os.path.exists(tmp):
                try: os.unlink(tmp)
                except Exception: pass
