# -*- coding: utf-8 -*-
"""
QQ 一键部署 —— 官方 NapCatAppImageBuild 一体式 AppImage（QQ NT + NapCat 官方打包）
============================================================================
为什么是它：QQ 官方 linuxqq rpm 的 CDN(qqdl.gtimg.cn QQNTV2 bucket) 对本机 IP 403 WAF 拦截，
NapCat-Installer 内嵌旧链接亦全部 404。NapCat 官方另有 AppImage 构建仓库
(NapNeko/NapCatAppImageBuild)，把 QQ NT 客户端 + NapCat v4 打成单个官方 AppImage，
GitHub 官方直链下载 → 彻底绕开 qqdl。来源=GitHub 官方 org，符合任务书白名单。

形态（已实测 v4.18.19）：
  AppImage 解包后内含 qq(204MB Electron QQ) + resources/app(已注入 loadNapCat.js) + napcat/
  运行 runtime/qq --no-sandbox：
    - NapCat 启动，控制台直接打印 ASCII 二维码 + 解码 URL + cache/qrcode.png
    - WebUI :6099（webui.json 可预写固定 token）
    - QQ 数据(登录态)落 ~/.config/QQ/nt_qq_<uin>，与系统 QQ 数据一致
    - NapCat 配置(webui.json/onebot11_*.json/logs)落 NAPCAT_WORKDIR
    - 登录成功后 NapCat 自动载入 config/onebot11.json 网络配置（HTTP server 3000 + HTTP 客户端推事件）

免重扫：登录态 = ~/.config/QQ/nt_qq_<uin>。重启时带 `-q <uin>` → NapCat 快速登录，不再弹码。
"""
import json, os, re, signal, subprocess, sys, time, glob, socket, hashlib, urllib.request

HOME = os.path.expanduser("~")
QQ_BASE = os.path.join(HOME, ".local/share/agent-terminal/qq")
APPIMAGE = os.path.join(QQ_BASE, "QQ-NapCat.AppImage")       # 官方原始资产(留档/校验/重解包)
RUNTIME = os.path.join(QQ_BASE, "runtime")                    # AppImage 解包产物(实际运行目录)
QQ_BIN = os.path.join(RUNTIME, "qq")
LOAD_NC = os.path.join(RUNTIME, "resources/app/loadNapCat.js")
WORKDIR = os.path.join(QQ_BASE, "napcat")                     # NAPCAT_WORKDIR → NapCat config/logs/cache
PID_FILE = os.path.join(QQ_BASE, "qq.pid")
LOG_FILE = os.path.join(QQ_BASE, "qq-run.log")
CFG_PATH = os.path.join(HOME, ".local/bin/term_agent/channel/config.json")
WEBUI_PORT = 6099
API_PORT = 3000
# 与 channel/adapters/qq.py 反向HTTP接收端一致
WEBHOOK_HOST, WEBHOOK_PORT = "127.0.0.1", 18086
WEBHOOK_PATH = "/onebot"
NC_VER = "4.18.19"  # 本会话核验版本（git 提交留档）

# ---------------- 基础 ----------------
def _qq_data_has_login():
    """QQ 登录态：NTQQ 数据目录存在带后缀 nt_qq_*（数字 uin 或 hash 目录均可）。
    注:新版 NTQQ 登录后目录为 nt_qq_<32位hash> 而非 nt_qq_<uin>，须一并识别。"""
    qq_conf = os.path.join(HOME, ".config/QQ")
    if os.path.isdir(qq_conf):
        try:
            for p in glob.glob(os.path.join(qq_conf, "nt_qq_*")):
                name = os.path.basename(p)
                if re.match(r"nt_qq_[A-Za-z0-9]+$", name):
                    return True
        except Exception:
            pass
    return False

def _uin_from_datadir():
    """优先从数据目录名取数字 uin（旧版 nt_qq_<uin>）。"""
    qq_conf = os.path.join(HOME, ".config/QQ")
    if os.path.isdir(qq_conf):
        for p in glob.glob(os.path.join(qq_conf, "nt_qq_*")):
            m = re.fullmatch(r"nt_qq_(\d+)", os.path.basename(p))
            if m:
                return m.group(1)
    return ""

def _uin_from_napcat_cfg():
    """hash 目录(NTQQ新版)时数字 uin 不在目录名 → 从 NapCat 配置反查账号。"""
    cfg_dir = os.path.join(WORKDIR, "config")
    if os.path.isdir(cfg_dir):
        try:
            for pat in ("onebot11_*.json", "napcat_*.json"):
                for p in glob.glob(os.path.join(cfg_dir, pat)):
                    m = re.search(r"(?:onebot11|napcat)_(\d+)\.json$", os.path.basename(p))
                    if m:
                        return m.group(1)
        except Exception:
            pass
    return ""

def _api_login_uin():
    """OneBot 正向 API 已就绪(=登录完成)时返回真实 QQ 号。"""
    if not _port_open("127.0.0.1", API_PORT):
        return ""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/get_login_info" % API_PORT,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            d = json.loads(r.read().decode("utf-8", "ignore") or "{}")
        uid = (d.get("data") or {}).get("user_id")
        return str(uid) if uid else ""
    except Exception:
        return ""

def _effective_uin():
    """当前有效 QQ 号：数据目录数字后缀 → NapCat 配置 → API 探测，逐级兜底。"""
    return (_uin_from_datadir() or _uin_from_napcat_cfg() or _api_login_uin())

def _logged_uin():
    return _effective_uin()

def _port_open(host, port, timeout=0.4):
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout); s.close(); return True
    except OSError:
        return False

def _proc_running(pid):
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False

def _read_pid():
    try:
        with open(PID_FILE) as f: return int(f.read().strip())
    except Exception:
        return None

# ---------------- 状态 ----------------
def qq_deploy_status():
    """installed / logged_in / running / webui / api 多维状态。"""
    installed = os.path.isfile(QQ_BIN) and os.path.isfile(LOAD_NC)
    logged_in = _qq_data_has_login()
    uin = _logged_uin()
    pid = _read_pid()
    running = bool(pid and _proc_running(pid))
    return {
        "installed": installed,
        "logged_in": logged_in,
        "uin": uin,
        "running": running,
        "pid": pid,
        "webui": _port_open("127.0.0.1", WEBUI_PORT),
        "api": _port_open("127.0.0.1", API_PORT),
    }

# ---------------- 部署（AppImage → runtime） ----------------
def _ensure_runtime(progress=print):
    """若 runtime 缺失或关键文件不齐，从官方 AppImage 重新解包。"""
    if os.path.isfile(QQ_BIN) and os.path.isfile(LOAD_NC):
        return True
    if not os.path.isfile(APPIMAGE):
        progress("  找不到官方 AppImage：%s" % APPIMAGE)
        progress("  请从 GitHub 官方仓库 NapNeko/NapCatAppImageBuild 获取后放至上述路径。")
        return False
    progress("  正在解包官方 AppImage（一次约 10~30s）…")
    os.makedirs(RUNTIME, exist_ok=True)
    # --appimage-extract 需在空目录进行（产物为 squashfs-root/）
    tmp = os.path.join(QQ_BASE, ".extract")
    if os.path.isdir(tmp):
        # 清空仅限自家目录内临时产物
        for n in os.listdir(tmp):
            p = os.path.join(tmp, n)
            subprocess.run(["rm", "-rf", p])
    else:
        os.makedirs(tmp)
    r = subprocess.run([APPIMAGE, "--appimage-extract"], cwd=tmp,
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isdir(os.path.join(tmp, "squashfs-root")):
        progress("  解包失败: " + (r.stderr or r.stdout or "")[-300:])
        return False
    # 搬入 runtime（自家临时产物，逐项覆盖）
    for n in os.listdir(os.path.join(tmp, "squashfs-root")):
        src = os.path.join(tmp, "squashfs-root", n)
        dst = os.path.join(RUNTIME, n)
        if os.path.isdir(src):
            os.makedirs(dst, exist_ok=True)
            subprocess.run(["cp", "-a", src + "/.", dst + "/"])
        else:
            subprocess.run(["cp", "-a", src, dst])
    # 清理临时解包
    subprocess.run(["rm", "-rf", tmp])
    ok = os.path.isfile(QQ_BIN) and os.path.isfile(LOAD_NC)
    progress("  解包完成: " + ("OK" if ok else "文件不齐，请检查"))
    return ok

def _write_onebot11_config():
    """预写 NAPCAT_WORKDIR/config/onebot11.json：
    - HTTP server :3000（正向 API，供 channel qq adapter 发消息）
    - HTTP client → 127.0.0.1:18086/onebot（反向，事件推给 channel qq adapter）
    登录成功后 NapCat 自动按 onebot11_<uin>.json（无则此默认）初始化。"""
    cfg_dir = os.path.join(WORKDIR, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    p = os.path.join(cfg_dir, "onebot11.json")
    if os.path.exists(p):
        return  # 已存在(可能是用户经 WebUI 改过的)，不覆盖
    conf = {
        "network": {
            "httpServers": [{
                "name": "httpServer",
                "enable": True,
                "port": API_PORT,
                "host": "127.0.0.1",
                "enableCors": True,
                "enableWebsocket": False,
                "messagePostFormat": "array",
                "token": "",
                "debug": False,
            }],
            "httpClients": [{
                "name": "httpClient2channel",
                "enable": True,
                "url": f"http://{WEBHOOK_HOST}:{WEBHOOK_PORT}{WEBHOOK_PATH}",
                "messagePostFormat": "array",
                "reportSelfMessage": False,
                "token": "",
                "debug": False,
            }],
            "httpSseServers": [],
            "websocketServers": [],
            "websocketClients": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    os.chmod(p, 0o600)

def _write_webui_config():
    """预写 webui.json：固定本机 token（不经公网，仅 127.0.0.1 可访问）。"""
    cfg_dir = os.path.join(WORKDIR, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    p = os.path.join(cfg_dir, "webui.json")
    if os.path.exists(p):
        return
    with open(p, "w", encoding="utf-8") as f:
        json.dump({
            "host": "127.0.0.1",
            "port": WEBUI_PORT,
            "token": "agent-channel",
            "loginRate": 10,
            "autoLoginAccount": "",
        }, f, ensure_ascii=False, indent=2)
    os.chmod(p, 0o600)

def qq_ensure(progress=print):
    """部署到可用：确保 runtime 解包 + NapCat 配置文件预写。"""
    if not _ensure_runtime(progress):
        return {"ok": False, "error": "AppImage 解包失败"}
    _write_onebot11_config()
    _write_webui_config()
    return {"ok": True}

# ---------------- 启停 ----------------
def _qq_running_pids():
    """返回真正在跑的 QQ(NapCat) 主进程 PID 列表。
    判定：cmdline 首 token basename=qq + 含 --no-sandbox + 非 --type= 子进程。
    （xvfb-run sh / zygote / node utility 均天然排除，绝不 pkill 自匹配）"""
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % name, "rb") as f:
                raw = f.read().decode("utf-8", "ignore").replace("\x00", " ")
        except Exception:
            continue
        parts = raw.split()
        if not parts:
            continue
        if os.path.basename(parts[0]) != "qq":
            continue
        if "--no-sandbox" not in parts:
            continue
        if any(x.startswith("--type=") for x in parts):
            continue
        out.append(int(name))
    return out

def _qq_pgids():
    """返回 QQ 主进程所在进程组 ID（去重）。用于 killpg 精确整组清理。"""
    pgs = set()
    for pid in _qq_running_pids():
        try:
            with open("/proc/%d/stat" % pid, encoding="utf-8") as f:
                s = f.read()
            m = re.search(r"\)\s+\S+\s+\d+\s+(\d+)", s)
            if m:
                pgs.add(int(m.group(1)))
        except Exception:
            pass
    return sorted(pgs)

def qq_stop():
    # 1) PID 文件指向存活进程 → 直接用其 pid（组长 xvfb sh）
    pid = _read_pid()
    killed = []
    if pid and _proc_running(pid):
        try:
            os.killpg(pid, signal.SIGTERM); killed.append(pid)
        except OSError:
            try: os.kill(pid, signal.SIGTERM); killed.append(pid)
            except OSError: pass
    # 2) 兜底：PID 文件失效/缺失 → 扫真实进程组（防重复清残留）
    for pg in _qq_pgids():
        if pg not in killed:
            try: os.killpg(pg, signal.SIGTERM); killed.append(pg)
            except OSError:
                try: os.kill(pg, signal.SIGTERM)
                except OSError: pass
    for _ in range(20):
        if not _qq_running_pids() and not (pid and _proc_running(pid)):
            break
        time.sleep(0.25)
    # 3) 仍存活 → 升级 KILL
    for pg in killed:
        try:
            os.killpg(pg, signal.SIGKILL)
        except OSError:
            try: os.kill(pg, signal.SIGKILL)
            except OSError: pass
    try: os.remove(PID_FILE)
    except OSError: pass
    return True

def _boot_cmd(need_scan, uin):
    """构造启动命令。登录过 → -q <uin> 快速登录；未登录 → 二维码登录。"""
    cmd = ["xvfb-run", "-a", QQ_BIN, "--no-sandbox"]
    if need_scan and uin:
        cmd += ["-q", uin]
    return cmd

def qq_start(need_scan=False, progress=print):
    """启动 NapCat。need_scan=True 且已登录 → 快速登录；未登录 → 打 QR。
    防重复：启动前 /proc 精确扫描真 QQ 主进程，存在即拒绝再起（不依赖 PID 文件）。"""
    alive = _qq_running_pids()
    if alive:
        progress("  NapCat 已在运行 (PID %s)。如需重启请先「停止 QQ」。" % alive[0])
        return True, None
    uin = _effective_uin()
    cmd = _boot_cmd(need_scan, uin)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    env = dict(os.environ)
    env["NAPCAT_WORKDIR"] = WORKDIR
    env.setdefault("PYTHONUNBUFFERED", "1")
    with open(LOG_FILE, "a", encoding="utf-8") as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))
    time.sleep(4)
    if not _proc_running(proc.pid):
        tail = ""
        try:
            with open(LOG_FILE, encoding="utf-8") as f:
                tail = "".join(f.readlines()[-12:])
        except Exception: pass
        progress("  NapCat 启动失败（进程已退出）。最近输出：\n" + tail)
        return False, proc
    progress("  NapCat 已启动 (PID %s)，日志: %s" % (proc.pid, LOG_FILE))
    return True, proc

# ---------------- 登录态写回 channel 配置 ----------------
def _load_cfg():
    try:
        with open(CFG_PATH, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {}

def _save_cfg(cfg):
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def qq_write_channel_cfg(uin):
    """把 bot_qq/onebot_http/反向HTTP 写进 channel/config.json 的 qq 段。"""
    cfg = _load_cfg()
    qq = cfg.get("qq") or {}
    qq["bot_qq"] = uin
    qq["onebot_http"] = f"http://127.0.0.1:{API_PORT}"
    qq["webhook_local"] = f"{WEBHOOK_HOST}:{WEBHOOK_PORT}"
    qq["webhook_path"] = WEBHOOK_PATH
    cfg["qq"] = qq
    ads = cfg.get("adapters") or []
    if "qq" not in ads:
        cfg["adapters"] = ads + ["qq"]
    _save_cfg(cfg)
    return True

# ---------------- 扫码登录主流程 ----------------
def qq_reset_login(progress=print):
    """重新登录：备份 QQ 数据目录后返回（下次启动即全新二维码登录）。"""
    qq_conf = os.path.join(HOME, ".config/QQ")
    if os.path.isdir(qq_conf):
        bak = qq_conf + ".backup-" + time.strftime("%Y%m%d-%H%M%S")
        os.rename(qq_conf, bak)
        progress("  QQ 数据已备份至: %s" % bak)
        progress("  提示：原账号数据仍在备份目录，确认新账号正常后可按需删除。")
    else:
        progress("  未发现 QQ 数据目录。")
    return True

def _render_qr(url, progress=print):
    """用 segno 在终端渲染 QQ 二维码（微信同款体验）；失败降级打印链接/图片路径。"""
    progress("")
    progress("  请使用手机 QQ 扫描下方二维码：")
    progress("  " + "-" * 48)
    shown = False
    try:
        import segno
        qr = segno.make(url, error='l')
        try:
            qr.terminal(out=sys.stdout, border=1, compact=True)
            shown = True
        except TypeError:
            qr.terminal(out=sys.stdout, border=1)
            shown = True
    except Exception:
        shown = False
    progress("  " + "-" * 48)
    if not shown:
        progress("  二维码渲染失败，请复制下方链接到浏览器/二维码工具（需手机 QQ 扫码）：")
    progress("  " + url)
    progress("  也可打开图片文件扫码：%s" % os.path.join(WORKDIR, "cache", "qrcode.png"))
    progress("")
    progress("  等待扫码…（二维码约 2 分钟自动刷新，请以最新一次显示为准）")
    progress("")

def _scan_latest_qr_url():
    """从 NapCat 运行日志取最新一条『二维码解码URL:』。"""
    try:
        with open(LOG_FILE, encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        m = re.findall(r"二维码解码URL:\s*(\S+)", txt)
        return m[-1] if m else ""
    except Exception:
        return ""

def qq_start_and_qr(need_scan=True, progress=print):
    """
    control 入口：
      已登录(数据目录有真实登录态) → 快速登录（免扫码，自动 -q <uin>）
      未登录 → 启动并解析 NapCat 二维码 URL → segno 直接渲染到终端 → 等手机 QQ 扫码
    阻塞等待登录成功。成功判定 = NapCat 正向 API get_login_info 返回真实 QQ 号
    （绝不把残留 config 反查当登录成功，否则会误报成功 → 二维码永不显示）。
    """
    dep = qq_deploy_status()
    if dep["logged_in"] and dep["uin"]:
        progress("  检测到 QQ 登录态 (nt_qq_%s)，走快速登录，免扫码。" % dep["uin"])
        ok, proc = qq_start(need_scan=True, progress=progress)
        if not ok: return False
        progress("  正在快速登录…")
    else:
        ok, proc = qq_start(need_scan=False, progress=progress)
        if not ok: return False
        progress("  正在准备二维码…")
    # ---- 等待登录成功（唯一真判定：NapCat API get_login_info 就绪）----
    deadline = time.time() + 180
    shown_url = None
    while time.time() < deadline:
        uid = _api_login_uin()
        if uid:
            qq_write_channel_cfg(uid)
            progress("  ● QQ 登录成功：%s" % uid)
            return True
        if proc is not None and not _proc_running(proc.pid):
            progress("  NapCat 进程已退出，登录流程中断。")
            return False
        # 未登录/扫码阶段：监视日志里的二维码 URL，变化即重新渲染(约2分钟自动刷新)
        # 快速登录若退化为弹码也会在此兜底渲染
        url = _scan_latest_qr_url()
        if url and url != shown_url:
            if shown_url:
                progress("")
                progress("  二维码已刷新，请用手机 QQ 扫上方最新二维码。")
            shown_url = url
            _render_qr(url, progress=progress)
        time.sleep(1.5)
    progress("  等待登录超时（3 分钟）。NapCat 仍在运行，可稍后手动扫码或重试。")
    return False
