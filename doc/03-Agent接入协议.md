# 03｜Agent 接入协议（谁都能接）

Channel 把 Agent 当**本地子进程**驱动。只要你的程序满足下面 4 条，就能被 QQ/微信遥控，
**不限语言 / 框架 / 模型来源**。参考最小实现：`agents/demo_agent.py`（~40 行）。

## 四条契约

### 1. 被 spawn（Channel 侧自动做，无需你管）
```python
Popen([sys.executable, agent_path], stdin=PIPE, stdout=PIPE,
      stderr=STDOUT, start_new_session=True, cwd=agent所在目录)
```
- 环境变量注入：
  - `CHANNEL_TASK_ID` = 会话 sid（可忽略）
  - `CHANNEL_SOCKET`  = 本会话专属回话 socket 绝对路径（**必须用它回话**）
- 进程会一直活着等待多轮任务，直到被 `/new` / manager 退出杀掉。

### 2. 从 stdin 读任务（单行）
- 每条 IM 消息 = **一行物理行**（Manager 已把换行折叠成空格，别期待多行）。
- 格式示例：
  `[图片] /tmp/.../1_photo.png 用户正文.... [运行环境] 当本轮任务完成时……`
- 处理完**不要退出**，回到 `input()`/`readline()` 等下一行 → 上下文延续。

### 3. 用 socket 主动回话（不是 print！）
最简单：把 `channel/cli/` 放进 PATH，然后：
```bash
channel-reply "最终回复文本"          # 唯一文本外发出口
channel-send-file "/绝对/路径/文件"    # 发文件（须在 allowed_file_dirs 内）
```
或自己裸连 socket 发 JSONLines 一帧：
```python
{"type": "reply", "text": "..."}
{"type": "file",  "path": "/绝对/路径"}
```
应答：`{"ok": true}` / `{"ok": false, "error": "..."}`。
socket 客户端约 15 行（参考 `agents/demo_agent.py` 的 `channel_send()`）。
可多次调用（先发多条文本、再发文件都行）。

### 4. 失败也要回话
任务失败请照样 `channel-reply` 报告失败原因。若进程直接崩溃，Manager 会自动回传
`[Channel 错误]` 并提示用户发 `/new`。

## 回话之外的纪律

- stdout/stderr 只当会话日志（`agent.out`），Manager **绝不**把 stdout 当回复外发。
- 别去直接调 QQ/微信/OneBot 的 API —— 那是适配器层的事。
- 收到任务文本里的 `[图片] /本地/路径` = 图片已由 Manager 下载到本机 inbox，你可直接读。
- 想做图像识别就自己处理图片文件（模型能力归 Agent，Channel 不管）。

## 进阶：Agent 跑在端口 / 远程？

Channel 默认只 spawn 本地命令。若你的 Agent 是跑在某端口 / HTTP / 远程机器上的服务，
两种接法（任选）：

1. **转发壳（推荐，不碰核心）**：写一个 ~20 行本地脚本做 Agent：
   从 stdin 读一行任务 → 把任务 POST 到你的 Agent 端口 → 拿回复调 `channel-reply` 回传。
   `config.json` 的 `agent` 指向这个壳脚本即可。示例骨架：

```python
#!/usr/bin/env python3
# forwarder.py —— 把 Channel 任务转发到 127.0.0.1:PORT 的 Agent 服务
import sys, json, urllib.request, os
def reply(text):
    # 复用 agents/demo_agent.py 的 channel_send 同款逻辑（env CHANNEL_SOCKET）
    ...
for line in sys.stdin:
    task = line.strip()
    if not task: continue
    req = urllib.request.Request("http://127.0.0.1:8000/chat",
          data=json.dumps({"task": task}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        ans = json.loads(r.read())["reply"]
    reply(ans)
```

2. **换驱动**：Agent 附着方式是可替换边界。改 `ChannelManager._spawn_session`，
   把「Popen 本地子进程」换成你的调用方式，保留会话/回话骨架即可。

> 说明：本项目刻意不做内置 HTTP-Agent 驱动——保持 Agent 附着模型的绝对简单；
> 谁有特殊需求谁自己在壳层解决，边界清晰。

## Agent 侧 CLI 细节

- `channel/cli/_channel_io.py` 的 `send_frame(typ, value)`：
  读 `CHANNEL_SOCKET` → 连 → 发一帧 → 等应答 → 按 ok 退出 0/1。
- socket 超时 10s；socket 不存在 → 明确报错退出 2。
- `channel-send-file` 支持中文引号剥离与 glob 展开，取第一个匹配的绝对路径。
