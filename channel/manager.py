#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent-Channel Manager（触发式常驻会话模型）
微信/QQ → Channel Manager → 每 conversation_id 一个常驻 Agent 进程（stdin 长开）→ channel-reply 回原会话

设计原则（任务书）：
- 不改 Agent 本体（term_agent.py 式单行任务 Agent）；不加数据库/Redis/daemon/systemd/全局队列。
- 触发式：只有收到 user 消息才动（写一行到 stdin）；其余时间完全静态，零心跳。
- 会话 = conversation_id：QQ 私聊 private:<qq> / 微信私聊 private:<wxid> / QQ 群 group:<gid> 各一上下文；
  同会话消息串行喂同一进程（延续上下文），跨会话进程并行隔离；只有 /new 显式开新上下文；不做主动过期。
- 唯一出口 = Agent 主动 channel-reply / channel-send-file；绝不把 stdout 当回复外发（stdout 只落会话日志 agent.out）。
- Agent 崩溃（rc!=0 且非 /new 主动终止）→ 回传错误告知，请用户发 /new 重开会话。
"""
import json, os, re, sys, uuid, socket, subprocess, threading, shutil, time, atexit, signal

REPO   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE   = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CHANNEL_CONFIG", os.path.join(HERE, "config.json"))

DEFAULT_CONFIG = {
    "agent": "./agents/demo_agent.py",
    "multimodal": True,
    "runtime_dir": "/tmp/agent-channel",
    "allowed_file_dirs": ["/tmp/agent-channel", REPO],
    "task_timeout": 1200,        # Agent 超时（秒）
    "max_file_mb": 200,          # channel-send-file 大小上限
    "exit_grace": 3,             # 任务结束后清理前等待秒
    "adapters": ["local"],       # 启用的适配器：local / wechat / qq
    "wechat": {},
    "qq": {},
}

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            cfg.update(json.load(open(CONFIG_PATH, encoding="utf-8")))
        except Exception as e:
            print(f"[Channel] 配置读取失败: {e}", file=sys.stderr)
    return cfg

def sanitize_text(t):
    """把消息压成单物理行（input() 每次只读一行），换行折叠为空格。"""
    if not t: return ""
    return re.sub(r"[ \t]*[\r\n]+[ \t]*", " ", str(t)).strip()

def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)

def get_agent_note():
    return ("\n[运行环境] 当本轮任务完成时，你必须立即用 Shell 调用 `channel-reply \"最终回复内容\"` "
            "把结果主动发回当前聊天——这是你唯一的外发出口，本通道不读取也不转发你的任何标准输出(print)。"
            "若最终结果需要附文件，用 `channel-send-file \"/绝对路径/文件\"`。两个命令都可多次调用。"
            "严禁直接调用微信/QQ/OneBot 等任何 IM API。若任务失败，也要用 channel-reply 如实报告失败原因。")

# ---------- 统一消息模型 ----------
def make_message(platform, conversation_id, sender_id, message_id, text, attachments=None):
    return {
        "platform": platform,
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "message_id": message_id,
        "text": text,
        "attachments": attachments or [],
    }

# ---------- Socket 协议服务 ----------
# Agent → Manager（JSON Lines, 一次一帧）:
#   {"type":"reply","text":"..."}
#   {"type":"file","path":"/abs/path"}
# Manager 应答: {"ok":true} 或 {"ok":false,"error":"..."}
class TaskSocket:
    def __init__(self, task, cfg):
        self.task, self.cfg = task, cfg
        self.path = task["socket_path"]
        self.server = None
        self.thread = None
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        os.chmod(self.path, 0o600)
        self.server.listen(8)
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        self.server.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(10)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk: break
                data += chunk
            line = data.split(b"\n", 1)[0].decode("utf-8", "ignore").strip()
            resp = self._dispatch(line)
            try:
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError:
                pass
        except Exception as e:
            try: conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode("utf-8"))
            except Exception: pass
        finally:
            conn.close()

    def _dispatch(self, line):
        if not line:
            return {"ok": False, "error": "空请求"}
        try:
            req = json.loads(line)
        except Exception:
            return {"ok": False, "error": "JSON 解析失败"}
        t = req.get("type")
        if t == "reply":
            text = sanitize_text(req.get("text"))
            if not text: return {"ok": False, "error": "空回复文本"}
            try:
                self.task["manager"].reply(self.task, text)
                with self.lock: self.task["replied"] = True
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        if t == "file":
            path = (req.get("path") or "").strip()
            try:
                self.task["manager"].send_file(self.task, path)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"未知类型 {t}"}

    def close(self):
        self._stop.set()
        if self.server:
            try: self.server.close()
            except Exception: pass
        if os.path.exists(self.path):
            try: os.unlink(self.path)
            except Exception: pass

# ---------- Channel Manager ----------
class ChannelManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tasks = {}            # 兼容旧字段（实际不再逐任务创建）
        self.sessions = {}         # conversation_id -> 常驻 Agent 会话 dict
        self.tlock = threading.RLock()   # 可重入：_get_session 持锁内再 _spawn_session 赋值
        self.adapters = {}
        self._load_runtime_dirs()

    # -- 目录 --
    def _base(self, *p): return os.path.join(self.cfg["runtime_dir"], *p)
    def inbox(self, tid): return self._base("inbox", tid)
    def runtime(self, tid): return self._base("runtime", tid)

    def _load_runtime_dirs(self):
        for d in ("inbox", "runtime"):
            os.makedirs(self._base(d), exist_ok=True)

    # -- 接收消息（由 Adapter 回调）--
    # ---------- 触发式常驻会话（零心跳）----------
    # 设计：每个 conversation_id 持有一个常驻 Agent 进程，stdin 长开。
    #   收到消息 → 把单行任务写进 stdin → Agent 处理完回到 input() 等下一行（空闲阻塞，CPU≈0）。
    #   同会话多条消息 → stdin 天然排队串行；跨会话各自进程天然并行。
    #   唯一出口 = Agent 主动 channel-reply / channel-send-file；本层绝不把 stdout 当回复外发。
    #   只有 /new 显式重置上下文（杀旧进程 → 下一条消息 spawn 全新 Agent）。
    def on_message(self, msg):
        text = sanitize_text(msg.get("text"))
        if text == "/new" or text.startswith("/new"):
            threading.Thread(target=self._cmd_new, args=(msg,), daemon=True).start()
            return
        if not (text or msg.get("attachments")):
            return  # 空消息忽略
        threading.Thread(target=self._deliver, args=(msg,), daemon=True).start()

    def _cmd_new(self, msg):
        cid = msg.get("conversation_id") or "default"
        dropped = self._drop_session(cid)
        self._log(f"[Channel] /new 会话已重置 cid={cid}" + (f"（旧 Agent 已终止）" if dropped else "（无旧会话）"))
        self._raw_send(msg, "已开启新会话。下一条消息将使用全新上下文。")

    def _deliver(self, msg):
        cid = msg.get("conversation_id") or "default"
        try:
            sess = self._get_session(msg, cid)
            line = self._compose_input(sess, msg)
            try:
                with sess["wlock"]:
                    sess["proc"].stdin.write(line + "\n")
                    sess["proc"].stdin.flush()
                self._log(f"[Channel] 已投喂 cid={cid} len={len(line)} → pid={sess['proc'].pid}")
            except (BrokenPipeError, OSError):
                # 进程已死 → 重建并重投一次
                self._log(f"[Channel] cid={cid} 进程已退出，重建后重投", err=True)
                self._drop_session(cid, silent=True)
                sess = self._spawn_session(msg, cid)
                with sess["wlock"]:
                    sess["proc"].stdin.write(line + "\n")
                    sess["proc"].stdin.flush()
                self._log(f"[Channel] 重建投喂成功 cid={cid} → pid={sess['proc'].pid}")
        except Exception as e:
            self._log(f"[Channel] 投递失败 cid={cid}: {e}", err=True)
            try:
                self._raw_send(msg, f"[Channel 错误] 消息投递失败：{e}")
            except Exception:
                pass

    def _get_session(self, msg, cid):
        with self.tlock:
            sess = self.sessions.get(cid)
            if sess and sess["proc"].poll() is None:
                return sess
            if sess:
                # 旧进程已死但未清理 → 摘除重建
                self.sessions.pop(cid, None)
                try: sess["tsock"].close()
                except Exception: pass
            return self._spawn_session(msg, cid)

    def _spawn_session(self, msg, cid):
        sid = uuid.uuid4().hex[:12]
        rt = self.runtime(f"sess_{sid}")
        os.makedirs(os.path.join(rt, "inbox"), exist_ok=True)
        # 路由骨架：回复/发文件只依赖这些字段
        s_msg = {
            "platform": msg.get("platform"),
            "conversation_id": cid,
            "sender_id": msg.get("sender_id"),
            "account_id": msg.get("account_id"),
            "message_id": msg.get("message_id"),
        }
        sess = {
            "task_id": sid,          # 兼容 _verify_path 等旧引用
            "cid": cid, "sid": sid,
            "msg": s_msg,
            "cfg": self.cfg, "manager": self,
            "socket_path": os.path.join(rt, "api.sock"),
            "replied": False, "stdout": "",
            "adapter": msg.get("platform"),
            "process": None, "proc": None,
            "wlock": threading.Lock(),
            "rt": rt, "inbox": os.path.join(rt, "inbox"),
            "log_path": os.path.join(rt, "agent.out"),
            "seq": 0, "dying": False,
        }
        tsock = TaskSocket(sess, self.cfg)
        sess["tsock"] = tsock
        tsock.start()
        env = dict(os.environ)
        env["CHANNEL_TASK_ID"] = sess["sid"]
        env["CHANNEL_SOCKET"]  = sess["socket_path"]
        agent = self.cfg["agent"]
        agent_path = agent if os.path.isabs(agent) else os.path.join(REPO, agent)
        proc = subprocess.Popen(
            [sys.executable, agent_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            cwd=os.path.dirname(agent_path), env=env, start_new_session=True,
        )
        sess["process"] = proc
        sess["proc"] = proc
        threading.Thread(target=self._drain, args=(cid, sess), daemon=True).start()
        with self.tlock: self.sessions[cid] = sess
        self._log(f"[Channel] 新会话 spawn cid={cid} pid={proc.pid} agent={agent_path}")
        return sess

    def _drain(self, cid, sess):
        """持续读 Agent stdout → 写会话日志（绝不外发）。EOF=进程退出 → 清理并视情回传。"""
        import collections
        tail = collections.deque(maxlen=80)
        try:
            out = sess["proc"].stdout
            with open(sess["log_path"], "a", encoding="utf-8", errors="replace") as lf:
                for line in out:
                    tail.append(line.rstrip("\n"))
                    lf.write(line)
                    lf.flush()
        except Exception as e:
            self._log(f"[Channel] drain 异常 cid={cid}: {e}", err=True)
        rc = sess["proc"].poll()
        killed = sess.get("dying")
        try: sess["tsock"].close()
        except Exception: pass
        with self.tlock:
            if self.sessions.get(cid) is sess:
                self.sessions.pop(cid, None)
        self._log(f"[Channel] Agent 退出 cid={cid} pid={sess['proc'].pid} rc={rc}" +
                  ("（/new 主动终止）" if killed else ""))
        if rc not in (0, -9, None) and not killed:
            # 非 /new 的非正常退出 → 回传提示（不是 stdout 兜底，是错误告知）
            try:
                self._raw_send(sess["msg"],
                    f"[Channel] Agent 异常退出(rc={rc})。请发送 /new 重开会话后继续。\n最近输出：\n"
                    + "\n".join(list(tail)[-12:]))
            except Exception as e:
                self._log(f"[Channel] 崩溃回传失败: {e}", err=True)

    def _compose_input(self, sess, msg):
        """构造本轮投喂的单行任务文本：附件说明 + 正文 + 协议强化。"""
        lines = []
        text = sanitize_text(msg.get("text"))
        sess["seq"] += 1
        paths = self._collect_attachments(msg, sess["inbox"], sess["seq"])
        if self.cfg.get("multimodal"):
            imgs = [q for q in paths if q.lower().endswith((".jpg",".jpeg",".png",".gif",".webp"))]
            files = [q for q in paths if q.lower() not in (".jpg",".jpeg",".png",".gif",".webp")]
            if imgs:  lines.append("[图片] " + " ".join(imgs))
            if files: lines.append("[附件] " + " ".join(files))
        else:
            if paths:
                lines.append("[附件] " + " ".join(paths))
        if text: lines.append(text)
        lines.append(get_agent_note())
        return sanitize_text(" ".join(lines))

    def _log(self, s, err=False):
        try:
            print(s, file=sys.stderr if err else sys.stdout, flush=True)
        except Exception:
            pass

    def _raw_send(self, msg, text):
        """把一段文本直接发回原会话（不经过任何 Agent 输出兜底逻辑）。"""
        ad = self.adapters.get((msg or {}).get("platform"))
        if ad is None:
            return
        try:
            ad.send_text(msg, self._clean_reply(text))
        except Exception as e:
            self._log(f"[Channel] 发送失败: {e}", err=True)

    def _drop_session(self, cid, silent=False):
        """终止并移除某会话的常驻 Agent。silent=不视作异常。"""
        with self.tlock:
            sess = self.sessions.pop(cid, None)
        if sess is None:
            return None
        sess["dying"] = True
        p = sess.get("proc")
        if p and p.poll() is None:
            try: os.killpg(p.pid, signal.SIGKILL)
            except Exception: pass
        try: sess["tsock"].close()
        except Exception: pass
        try: shutil.rmtree(sess["rt"], ignore_errors=True)
        except Exception: pass
        return sess

    def _collect_attachments(self, msg, inbox, seq):
        """把消息里的附件(网络URL/本地路径)放入会话 inbox 的轮次子目录，返回本地绝对路径列表。"""
        os.makedirs(inbox, exist_ok=True)
        out = []
        att = msg.get("attachments") or []
        if not att:
            return out
        sub = os.path.join(inbox, str(seq))
        os.makedirs(sub, exist_ok=True)
        for i, a in enumerate(att):
            if not a: continue
            try:
                if a.startswith(("http://", "https://")):
                    import urllib.request
                    name = os.path.basename(a.split("?")[0]) or f"att_{i}"
                    dest = os.path.join(sub, f"{i}_{name}")
                    urllib.request.urlretrieve(a, dest)
                    out.append(dest)
                elif os.path.isfile(a):
                    dest = os.path.join(sub, f"{i}_{os.path.basename(a)}")
                    shutil.copy2(a, dest)
                    out.append(dest)
            except Exception as e:
                print(f"[Channel] 附件下载失败 {a}: {e}", file=sys.stderr)
        return out

    # -- 发送到聊天 --
    def _clean_reply(self, text):
        """清洗 Agent 回复：去掉通道侧混入的 '你> ' 提示符残影/前后空行。"""
        if not text: return text
        lines = text.split("\n")
        out = []
        for ln in lines:
            st = ln.strip()
            if st == "你>":     # 丢弃纯提示符残影行
                continue
            out.append(ln)
        t = "\n".join(out).strip()
        t = re.sub(r"(?:^|\n)\s*你>\s*", "", t)   # 行首兜底
        t = t.replace("\\n", "\n")                # 字面 \n 转真实换行
        return t.strip()

    def reply(self, task, text):
        text = self._clean_reply(text)
        ad = self.adapters.get(task["msg"]["platform"])
        if ad is None: raise RuntimeError(f"无适配器: {task['msg']['platform']}")
        ad.send_text(task["msg"], text)

    def send_file(self, task, raw_path):
        path = self._verify_path(task, raw_path)
        ad = self.adapters.get(task["msg"]["platform"])
        if ad is None: raise RuntimeError(f"无适配器: {task['msg']['platform']}")
        ad.send_file(task["msg"], path)

    def _verify_path(self, task, raw_path):
        path = os.path.realpath(os.path.expanduser(raw_path or ""))
        if not os.path.exists(path):
            raise RuntimeError(f"文件不存在: {raw_path}")
        if not os.access(path, os.R_OK):
            raise RuntimeError(f"文件不可读: {raw_path}")
        if not os.path.isfile(path):
            raise RuntimeError(f"不是普通文件: {raw_path}")
        allow = [os.path.realpath(d) for d in self.cfg.get("allowed_file_dirs", [])]
        allow.append(os.path.realpath(self.runtime(task.get("task_id") or "sess_x")))  # 兼容会话 runtime 前缀
        if not any(path == a or path.startswith(a + os.sep) for a in allow):
            raise RuntimeError(f"路径不在允许目录内: {raw_path}")
        if os.path.getsize(path) > self.cfg.get("max_file_mb", 200) * 1024 * 1024:
            raise RuntimeError(f"文件过大: {raw_path}")
        return path

    def _send_error(self, task, msg):
        try:
            ad = self.adapters.get(task["msg"]["platform"])
            if ad: ad.send_text(task["msg"], f"[Channel 错误] {msg}")
        except Exception as e:
            print(f"[Channel] 错误回传失败: {e}", file=sys.stderr)

    # -- Adapter 注册/启动 --
    def load_adapters(self):
        from adapters import create_adapters
        for name, ad in create_adapters(self.cfg).items():
            if name not in self.cfg.get("adapters", []):
                continue
            try:
                ad.attach(self)
                self.adapters[name] = ad
            except Exception as e:
                # 单个适配器启动失败(如端口被占)绝不拖垮 manager 与其他通道
                print(f"[Channel] 适配器 {name} 启动失败，已跳过（其余通道不受影响）: {e}",
                      file=sys.stderr, flush=True)

    def run(self):
        self.load_adapters()
        print(f"[Channel] Manager 启动 | 适配器: {list(self.adapters)} | runtime: {self.cfg['runtime_dir']}", flush=True)
        self._install_signal()
        self._keep_alive()
        self._shutdown_adapters()

    def _install_signal(self):
        import signal as _sig
        try:
            _sig.signal(_sig.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
            _sig.signal(_sig.SIGINT,  lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
        except Exception:
            pass

    def _keep_alive(self):
        # 纯静态驻留：主线程阻塞在 Event，不跑任何周期任务/心跳。
        self._stop_evt = threading.Event()
        try:
            self._stop_evt.wait()
        except KeyboardInterrupt:
            pass
        print("\n[Channel] 正在退出，终止运行中的会话…", flush=True)
        self._kill_all_tasks()

    def _kill_all_tasks(self):
        """退出时清杀所有常驻 Agent 子进程，防止 manager 停止后任务成孤儿残留。"""
        with self.tlock:
            sesss = list(self.sessions.values())
            legacy = [t.get("process") for t in list(self.tasks.values()) if t.get("process")]
        killed = 0
        for sess in sesss:
            p = sess.get("proc") or sess.get("process")
            try:
                if p and p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)   # Agent start_new_session=True → killpg 整组
                    killed += 1
            except Exception:
                pass
            try: sess.get("tsock") and sess["tsock"].close()
            except Exception: pass
        for proc in legacy:
            try:
                if proc and proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL); killed += 1
            except Exception:
                pass
        if killed:
            print(f"[Channel] 已终止 {killed} 个运行中的 Agent 进程", flush=True)
        time.sleep(1)   # 给清理线程收尾时间

    def _shutdown_adapters(self):
        for name, ad in list(self.adapters.items()):
            try:
                ad.stop()
            except Exception as e:
                print(f"[Channel] 适配器 {name} 停止失败: {e}", file=sys.stderr, flush=True)

def main():
    cfg = load_config()
    m = ChannelManager(cfg)
    if "--test" in sys.argv:
        m.load_adapters()
        idx = sys.argv.index("--test")
        text = " ".join(sys.argv[idx+1:]).strip() or "测试任务"
        print(f"[Channel] 注入本地测试消息: {text}")
        m.on_message(make_message("local", "test-conv", "local-user", "m1", text, _attachments_from_args(cfg)))
        # 常驻模型：等待该会话 agent 输出稳定（连续 3s 无新输出视为本轮完成），上限 task_timeout
        import time as _t
        t0 = _t.time()
        limit = min(int(cfg.get("task_timeout", 1200)), 180)
        last_sz, stable = -1, 0
        sess0 = None
        while _t.time() - t0 < limit:
            with m.tlock:
                sess0 = next(iter(m.sessions.values())) if m.sessions else None
            if sess0:
                lp = sess0.get("log_path") or ""
                sz = os.path.getsize(lp) if (lp and os.path.exists(lp)) else 0
                if sz == last_sz and sz > 0:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    last_sz, stable = sz, 0
            _t.sleep(1)
        m._kill_all_tasks()
        # 打印 local 回复日志尾部供验收
        outlog = "/tmp/agent-channel/outbox-local.log"
        try:
            if os.path.exists(outlog):
                print("\n===== outbox-local.log 尾部 =====", flush=True)
                print(open(outlog, encoding="utf-8").read().strip()[-1200:], flush=True)
        except Exception:
            pass
        print("\n[Channel] 测试完成", flush=True)
        return
    if "--serve" in sys.argv:
        m.run(); return
    # 默认 serve
    m.run()

def _attachments_from_args(cfg):
    out = []
    for a in sys.argv[1:]:
        if a.startswith("--test"): continue
        if a.startswith("--"): continue
        out.append(a)
    att = []
    for p in out:
        if os.path.exists(p): att.append(os.path.realpath(p))
    return att

if __name__ == "__main__":
    main()
