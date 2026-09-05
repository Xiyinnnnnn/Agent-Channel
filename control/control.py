#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent-Channel 傻瓜控制中心（零第三方依赖，标准库 TUI）

职责（只做控制层，不碰 Agent Core / Channel 核心逻辑）：
  - start / stop / status  Channel Manager（PID 文件方式，防重复启动）
  - 2=微信登录 3=QQ登录：真实扫码（微信=自研 iLink QR；QQ=自动准备官方 NapCat + QR）
  - 登录态持久化于 HOME，重启免重扫
  - 多模态 ON/OFF（持久化到现有 channel/config.json，只影响新任务）
  - 查看最近日志
  - 桌面统一入口：Agent Channel

启动：python3 control/control.py
"""
import json, os, re, signal, socket, subprocess, sys, time

# ---- 路径：全部基于本文件自身位置定位，不硬编码用户名 ----
HERE   = os.path.dirname(os.path.abspath(__file__))          # repo/control
REPO   = os.path.dirname(HERE)                               # repo
CH_DIR = os.path.join(REPO, "channel")                       # repo/channel
CONFIG = os.path.join(CH_DIR, "config.json")                 # 复用现有配置（唯一一份）
MANAGER = os.path.join(CH_DIR, "manager.py")
LOG_DIR = "/tmp/agent-channel"
PID_FILE = os.path.join(LOG_DIR, "channel.pid")
STATE_FILE = os.path.join(LOG_DIR, "channel.state.json")     # 运行态元数据（PID/启动时间/适配器）
LOG_FILE = os.path.join(LOG_DIR, "channel.log")
OUTBOX_LOG = os.path.join(LOG_DIR, "outbox-local.log")

# 让 control 可直接 import channel 下的 ilink（微信登录）
if CH_DIR and CH_DIR not in sys.path:
    sys.path.insert(0, CH_DIR)

GREEN, YELLOW, RED, CYAN, BOLD, NC = "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[1m", "\033[0m"

def c(s, color): return f"{color}{s}{NC}"

def _dw(s):
    """等宽终端下按显示宽度取字符串（中文/全角算2）。"""
    w = 0
    for ch in s:
        if ord(ch) > 0x2E7F:  # 非 ASCII 可见区按全角 2 计
            w += 2
        else:
            w += 1
    return w

def _pad(s, width):
    return s + " " * max(0, width - _dw(s))

def _strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)

# ---------------- 配置 ----------------
def load_config():
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(c(f"[配置读取失败] {e}", RED))
    return {}

def save_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(CONFIG, 0o600)

# ---------------- Channel 进程状态 ----------------
def _read_pid():
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None

def _pid_alive(pid):
    if not pid: return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def _is_manager(pid):
    """确认 pid 确实是 channel/manager.py（防误杀别的进程）。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode("utf-8", "ignore").replace("\x00", " ")
        return "channel/manager.py" in cmd or "manager.py" in cmd
    except Exception:
        return False

def _find_manager_proc():
    """遍历 /proc 找真正的 channel manager 进程。
    只认 python 解释器执行且 argv 含 channel/manager.py 的进程，
    绝不匹配 shell（否则 pgrep -f 会自匹配 'manager.py' 字符串造成误判）。"""
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as f:
                raw = f.read().decode("utf-8", "ignore").replace("\x00", " ")
        except Exception:
            continue
        parts = raw.split()
        if not parts:
            continue
        exe = os.path.basename(parts[0]).lower()
        if "python" not in exe and "pypy" not in exe:
            continue
        args = [a for a in parts[1:] if not a.startswith("-") and "/" in a]
        if not any(a.endswith("manager.py") and "channel" in a for a in args):
            continue
        if "--serve" in parts or "--test" in parts:
            return int(name)
    return None

def channel_status():
    """返回 (state, info)；state ∈ running/stopped/stale"""
    pid = _read_pid()
    if pid and _pid_alive(pid):
        if _is_manager(pid):
            return "running", {"pid": pid}
        # PID 活着但不是 manager（被别的进程占用）→ 视为残留
        return "stale", {"pid": pid, "reason": "PID 文件指向非 manager 进程"}
    if pid:
        return "stale", {"pid": pid, "reason": "残留 PID（进程已退出）"}
    # 没有 pid 文件，再兜底找一次（例如手动起的 manager / PID 文件被清）
    try:
        m = _find_manager_proc()
        if m and m != os.getpid():
            return "running", {"pid": m, "note": "无PID文件但检测到运行中的 manager"}
    except Exception:
        pass
    return "stopped", {}

def _clear_stale():
    for f in (PID_FILE, STATE_FILE):
        try:
            if os.path.exists(f): os.remove(f)
        except Exception:
            pass

# ---------------- 日志（复用 /tmp/agent-channel） ----------------
def tail_file(path, n=40):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return "(暂无日志)"

# ---------------- 微信 / QQ 登录状态（真实凭证判定） ----------------
def _port_open(host, port, timeout=0.5):
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False

def _wx_accounts():
    """微信已登录账号列表（持久目录）。"""
    try:
        from ilink import accounts as acc
        return acc.list_account_ids()
    except Exception:
        return []

def wx_login_state():
    """微信：持久凭证目录存在账号=已登录。"""
    ids = _wx_accounts()
    if ids:
        return {"level": "ok", "accounts": ids}
    return {"level": "none", "hint": "未登录"}

def _qq_data_has_login():
    """QQ 登录态检测：
    ① NTQQ 数据目录存在 nt_qq_<uin>（扫码登录后生成）
    ② NapCat 配置目录存在 onebot11_<uin>.json
    """
    import glob
    qq_conf = os.path.expanduser("~/.config/QQ")
    if os.path.isdir(qq_conf):
        try:
            hits = glob.glob(os.path.join(qq_conf, "nt_qq_*"))
            if hits: return True
        except Exception: pass
    napcat = os.path.expanduser("~/Napcat/opt/QQ/resources/app/app_launcher/napcat/config")
    if os.path.isdir(napcat):
        try:
            hits = glob.glob(os.path.join(napcat, "onebot11_*.json"))
            if hits: return True
        except Exception: pass
    return False

def _qq_process_running():
    # 官方 AppImage runtime 形态：.../agent-terminal/qq/runtime/qq --no-sandbox
    try:
        out = subprocess.run(["pgrep", "-f", "qq/runtime/qq --no-sandbox"],
                             capture_output=True, text=True).stdout.strip()
        if out:
            return True
        out = subprocess.run(["pgrep", "-f", "napcat.mjs"],
                             capture_output=True, text=True).stdout.strip()
        return bool(out)
    except Exception:
        return False

def _qq_deployed():
    base = os.path.expanduser("~/.local/share/agent-terminal/qq")
    qq = os.path.join(base, "runtime/qq")
    nc = os.path.join(base, "runtime/resources/app/loadNapCat.js")
    return os.path.exists(qq) and os.path.exists(nc)

def qq_login_state():
    """QQ：官方 AppImage 部署 + 登录态 + 进程 三维判定。"""
    cfg = load_config()
    qq = cfg.get("qq") or {}
    deployed = _qq_deployed()
    has_login = _qq_data_has_login() or bool(qq.get("bot_qq"))
    running = _qq_process_running()
    if deployed and has_login and running:
        return {"level": "ok", "deployed": True, "running": True}
    if deployed and has_login and not running:
        return {"level": "half", "deployed": True, "has_login": True,
                "running": False, "hint": "已登录 → 选 [3] 一键快速登录(免扫码)"}
    if deployed and not has_login:
        return {"level": "half", "deployed": True, "has_login": False,
                "running": running, "hint": "NapCat 已就绪 → 选 [3] 扫码登录"}
    return {"level": "none", "hint": "QQ 未部署 → 选 [3] 自动部署+扫码"}

def adapter_login_state():
    return {"qq": qq_login_state(), "wechat": wx_login_state()}

# ---------------- start / stop ----------------
def start_channel():
    state, info = channel_status()
    if state == "running":
        print(c(f"Channel 已在运行中 (PID {info.get('pid')})，无需重复启动。", GREEN))
        return False
    if state == "stale":
        print(c(f"检测到残留状态：{info.get('reason')} → 自动清理。", YELLOW))
        _clear_stale()
    os.makedirs(LOG_DIR, exist_ok=True)
    py = sys.executable
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    # nohup 风格：脱离终端，日志落 /tmp/agent-channel/channel.log
    with open(LOG_FILE, "a", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            [py, MANAGER, "--serve"],
            cwd=CH_DIR, env=env,
            stdout=logf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    # 写 PID 文件
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    # 等 1.2s 确认没立刻崩
    time.sleep(1.2)
    if proc.poll() is not None:
        _clear_stale()
        tail = tail_file(LOG_FILE, 10)
        print(c("Channel 启动失败（进程已退出）。最近日志：", RED))
        print(tail)
        return False
    print(c(f"Channel 已启动 (PID {proc.pid})。", GREEN))
    print("  日志: " + LOG_FILE)
    return True

def stop_channel():
    state, info = channel_status()
    if state == "stopped":
        print(c("Channel 未运行。", YELLOW))
        return False
    if state == "stale":
        print(c(f"残留状态：{info.get('reason')} → 清理，无需停止。", YELLOW))
        _clear_stale()
        return True
    pid = info["pid"]
    print(c(f"正在停止 Channel (PID {pid})…", CYAN))
    # 1) SIGTERM 正常关闭（manager 的 KeyboardInterrupt 处理会触发退出打印）
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(c(f"发送 SIGTERM 失败: {e}", RED))
    # 2) 等待正常退出（最多 5s）
    for _ in range(25):
        if not _pid_alive(pid):
            break
        time.sleep(0.2)
    # 3) 必要时 terminate → 最后才是 kill
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        time.sleep(1)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        print(c("进程未在宽限期内退出，已强制结束。", YELLOW))
    else:
        print(c("Channel 已正常退出。", GREEN))
    # 4) 清理 PID / 状态
    _clear_stale()
    return True

def multimodal_state():
    cfg = load_config()
    return bool(cfg.get("multimodal", True))

def toggle_multimodal():
    cfg = load_config()
    cur = bool(cfg.get("multimodal", True))
    cfg["multimodal"] = not cur
    save_config(cfg)
    now = "ON" if cfg["multimodal"] else "OFF"
    print(c(f"多模态已切换为 {now}。", GREEN))
    print(c("  仅对新启动的 Agent 任务生效，当前正在执行的任务不受影响。", YELLOW))
    return cfg["multimodal"]

# ---------------- 首页 ----------------
def _dot(ok):
    return c("●", GREEN) if ok else c("○", RED)

def status_line():
    st, info = channel_status()
    ls = adapter_login_state()
    qq, wx = ls.get("qq"), ls.get("wechat")
    mm = multimodal_state()
    return st, info, qq, wx, mm

def render():
    st, info, qq, wx, mm = status_line()
    # Channel 行
    if st == "running":
        ch_line = c("● 运行中", GREEN) + c(f"   (PID {info.get('pid')})", NC)
    elif st == "stale":
        ch_line = c("! 异常(残留)", RED) + c(f"   {info.get('reason','')}", NC)
    else:
        ch_line = c("○ 未运行", RED)
    # 登录状态行
    qq_lv = qq.get("level", "none")
    wx_lv = wx.get("level", "none")
    if qq_lv == "ok": qq_line = c("● 已登录", GREEN)
    elif qq_lv == "half":
        if qq.get("has_login"): qq_line = c("● 已登录 · 未运行", YELLOW)   # 选[3]快速登录
        else: qq_line = c("○ 未登录 · 已就绪", YELLOW)                     # 选[3]扫码登录
    else: qq_line = c("○ 未登录", RED)
    if wx_lv == "ok": wx_line = c("● 已登录", GREEN)
    elif wx_lv == "half": wx_line = c("! 异常", YELLOW)
    else: wx_line = c("○ 未登录", RED)
    mm_line = c("● ON", GREEN) if mm else c("○ OFF", RED)

    w = 56
    print(c("╔" + "═"*(w-2) + "╗", CYAN))
    title = " Agent-Channel "
    pad = w - 2 - len(title)
    print(c("║" + " "*max(0,(pad//2)) + title + " "*max(0,pad-pad//2) + "║", CYAN))
    print(c("╠" + "═"*(w-2) + "╣", CYAN))
    def row(k, v):
        plain = _strip_ansi(f" {k}") + _strip_ansi(v)
        kk = " " + k
        body = kk + " " + v
        fill = w - 2 - _dw(plain) - 1   # 右边框前至少1空格
        print(c("║" + body + " " * max(0, fill) + "║", CYAN))
    row("Channel", ch_line)
    row("QQ", qq_line)
    row("微信", wx_line)
    row("多模态", mm_line)
    print(c("╠" + "═"*(w-2) + "╣", CYAN))
    menu = [
        ("1", "启动 / 停止 Channel"),
        ("2", "微信登录"),
        ("3", "QQ登录"),
        ("4", "多模态 ON / OFF"),
        ("5", "刷新状态"),
        ("0", "退出"),
    ]
    for k, t in menu:
        row(k, c(t, NC))
    print(c("╚" + "═"*(w-2) + "╝", CYAN))
    if st != "running":
        print(c("提示：Channel 未运行 → 按 [1] 启动。启动后微信/QQ 消息才会触发 Agent。", YELLOW))
    print(c("按数字直接进入对应功能：未登录则直接拉二维码，已登录可 [r] 重新登录。", NC))
    print()

def _pause():
    try:
        input(c("按回车返回…", CYAN))
    except EOFError:
        pass

def _done(ok=True):
    """登录流程收尾：成功 → 短暂提示后自动回主页面（用户无需再按键）；
    失败/未完成 → 保留按回车返回（避免错误信息一闪而过）。"""
    if not ok:
        _pause(); return
    print()
    sys.stdout.write(c("● 操作完成，", GREEN) + c("2.5 秒后自动返回主页面…", CYAN))
    sys.stdout.flush()
    try:
        for _ in range(5):
            time.sleep(0.5)
            sys.stdout.write("\r  " + c("即将自动返回主页面…  ", CYAN))
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    print()

def act_start_stop():
    st, info, *_ = status_line()
    if st == "running":
        stop_channel()
    else:
        start_channel()
    _pause()

def _enable_adapter(name):
    """把 name 写进 config.json 的 adapters（若未启用）。"""
    cfg = load_config()
    ads = cfg.get("adapters") or []
    if name not in ads:
        cfg["adapters"] = ads + [name]
        save_config(cfg)
    return cfg

def act_wx_login():
    """2. 微信登录：已登录→提示[r]重登；未登录→直接拉 QR 扫码。"""
    st, info, qq, wx, mm = status_line()
    if wx.get("level") == "ok":
        print()
        print(c("微信已登录。", GREEN), "账号:", ", ".join(wx.get("accounts") or []))
        print("  [r] 重新登录（重新扫码）    [0] 返回")
        k = input("  选择: ").strip().lower()
        if k != "r":
            return
        print(c("  重新登录将覆盖当前微信账号的凭证。开始…", YELLOW))
    print()
    print(c("正在启动微信扫码登录…", CYAN))
    print("  （iLink 通道 · 自动拉取二维码 → 手机微信扫码 → 凭证自动保存，重启免重扫）")
    print()
    try:
        from ilink import weixin_bot as wxbot
        ok, res = wxbot.do_qr_login(total_timeout_s=480)
    except Exception as e:
        print(c(f"  微信登录启动失败: {e}", RED))
        _pause(); return
    if ok:
        print()
        print(c("● 微信登录成功！", GREEN))
        print("  账号:", res.get("saved_account_id") or res.get("account_id") or "(见日志)")
        if res.get("message"): print("  " + res["message"])
        # 自动启用 wechat 适配器 + 重启 Channel 使轮询生效
        try:
            _enable_adapter("wechat")
        except Exception as e:
            print(c(f"  写入配置失败: {e}", RED))
        print()
        print(c("提示：已自动启用「微信」适配器。若 Channel 正在运行，重启后开始收微信消息。", YELLOW))
    else:
        msg = res.get("message") if isinstance(res, dict) else str(res)
        if isinstance(res, dict) and res.get("alreadyConnected"):
            print(c("  " + msg, GREEN))
            _enable_adapter("wechat")
        else:
            print(c("  登录未完成: " + str(msg), RED))
    _done(ok)

def act_qq_login():
    """3. QQ登录：官方 AppImage 自动部署 → 扫码/快速登录 → ●已登录。"""
    st, info, qq, wx, mm = status_line()
    qs = qq.get("level")
    if qs == "ok":
        print()
        print(c("QQ 已登录且 NapCat 运行中。", GREEN))
        print("  [r] 重新扫码登录    [s] 停止 QQ    [0] 返回")
        k = input("  选择: ").strip().lower()
        if k == "r":
            pass  # 继续向下：先备份数据再扫新码
        elif k == "s":
            from qqdeploy import qq_stop
            qq_stop()
            print(c("QQ/NapCat 已停止。", GREEN))
            _pause(); return
        else:
            return
    print()
    print(c("正在检查 QQ 环境…", CYAN))
    try:
        from qqdeploy import (qq_deploy_status, qq_ensure, qq_start_and_qr,
                              qq_reset_login, qq_stop)
    except Exception as e:
        print(c(f"  QQ 部署模块缺失: {e}", RED))
        _pause(); return
    dep = qq_deploy_status()
    if not dep["installed"]:
        print(c("正在准备官方 NapCat（首次需下载 QQ + NapCat AppImage，稍候）…", CYAN))
        r = qq_ensure()
        if not r["ok"]:
            print(c("  部署失败: " + r["error"], RED))
            _pause(); return
        print(c("  NapCat 部署完成。", GREEN))
        dep = qq_deploy_status()
    if qs == "ok" and dep.get("running"):
        # [r] 重新登录：先停 → 备份数据 → 全新扫码
        print(c("  正在停止 NapCat…", YELLOW))
        qq_stop()
        qq_reset_login()
        print(c("  旧登录态已备份移除，开始全新扫码。", YELLOW))
    print()
    ok = qq_start_and_qr(need_scan=True)   # 内部按登录态自动分流：有态→快速登录/无态→QR
    if ok:
        print()
        print(c("● QQ 登录完成，NapCat 运行中。", GREEN))
        print("  - 免重扫：重启后选 [3] 即自动快速登录")
        print("  - 已写入 channel/config.json (qq 适配器自动启用)")
        print("  - 收发就绪：HTTP :3000 正向API / 事件推 127.0.0.1:18086/onebot")
        _enable_adapter("qq")
    else:
        print()
        print(c("QQ 登录流程未完成（详见上方 NapCat 输出）。", RED))
        print(c("  NapCat 仍在运行可继续扫码；或重启后再试。", YELLOW))
    _done(ok)

def act_log():
    print()
    print(c("最近日志（/tmp/agent-channel/channel.log 末尾 40 行）", BOLD))
    print("-"*56)
    print(tail_file(LOG_FILE, 40))
    if os.path.exists(OUTBOX_LOG):
        print(c("本地回复记录 outbox-local.log 末尾 10 行", BOLD))
        print("-"*56)
        print(tail_file(OUTBOX_LOG, 10))
    _pause()

def main():
    # 兼容单参数子命令：control.py --status / --start / --stop（给脚本/快捷用）
    if len(sys.argv) > 1:
        a = sys.argv[1]
        if a in ("--status", "status"):
            st, info = channel_status()
            print(json.dumps({"state": st, **info}, ensure_ascii=False))
            return
        if a in ("--start", "start"):
            sys.exit(0 if start_channel() else 1)
        if a in ("--stop", "stop"):
            sys.exit(0 if stop_channel() else 1)
    while True:
        os.system("clear" if os.name == "posix" else "cls")
        render()
        try:
            k = input("  选择 [1-5 / 0]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  再见。"); return
        if k == "1": act_start_stop()
        elif k == "2": act_wx_login()
        elif k == "3": act_qq_login()
        elif k == "4": toggle_multimodal(); _pause()
        elif k == "5": continue          # 刷新=回到 render 顶部（自然循环）
        elif k == "0": print("  再见。"); return
        else: print(c("  无效输入，请按菜单数字。", YELLOW)); time.sleep(0.8)

if __name__ == "__main__":
    main()
