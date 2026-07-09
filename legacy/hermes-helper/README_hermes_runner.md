# 用途

这个脚本用来：

- 直接初始化 Hermes 的 `AIAgent`
- 调用一次 `run_conversation(user_message=...)`
- 在主回复生成后，手动启动一次 background review
- 等待 review 结束
- 最后只把 `run_conversation()` 的主回复写到标准输出

review 的摘要或 review 自己的文本不会输出给调用方。

# 文件

- 主脚本：`tmp/hermes_runner.py`
- 调用示例：`tmp/call_hermes_runner.py`

# 固定初始化参数

脚本内部固定使用这些 `AIAgent(...)` 参数：

- `quiet_mode=True`
- `skip_context_files=True`
- `skip_memory=False`
- `max_iterations=64`

你只需要从外部传：

- `model`
- `base_url`
- `api_key`
- `session_id`
- `max_tokens`
- `user_message`
- `workspace`

# 参数

必传参数：

- `--hermes-agent-dir`
  - Hermes 仓库路径
  - 例：`/home/iklare/.hermes/hermes-agent`
- `--user-message`
  - 本轮发送给 `run_conversation()` 的提示词
- `--model`
  - 传给 `AIAgent(model=...)`
- `--base-url`
  - 传给 `AIAgent(base_url=...)`
- `--api-key`
  - 传给 `AIAgent(api_key=...)`
- `--session-id`
  - 传给 `AIAgent(session_id=...)`
- `--max-tokens`
  - 传给 `AIAgent(max_tokens=...)`

可选参数：

- `--profile-home`
  - 显式指定 `HERMES_HOME`
  - 如果传了，脚本会先设置 `os.environ["HERMES_HOME"]`
  - 如果不传，脚本就沿用当前进程已有的 `HERMES_HOME`
- `--workspace`
  - 显式指定工作区目录
  - 脚本会先 `chdir` 到这个目录，再创建 `AIAgent`
  - 同时会设置 `TERMINAL_CWD=<workspace>`
- `--no-review-skills`
  - 关闭 skill background review
  - 默认不传时，skill review 是开启的
- `--no-review-memory`
  - 关闭 memory background review
  - 默认不传时，memory review 也是开启的
  - 但当前脚本固定 `skip_memory=True`，所以 memory review 通常不会比 skill review 更有价值

# HERMES_HOME 和 profile

Hermes 的 profile 本质上就是不同的 `HERMES_HOME` 目录。

常见路径规则：

- 默认 profile：`~/.hermes`
- 命名 profile：`~/.hermes/profiles/<name>`

例如：

- `~/.hermes/profiles/coder`
- `~/.hermes/profiles/reviewer`

这个脚本不会自动根据 profile 名帮你推路径，它只认最终的 `HERMES_HOME` 路径。

你有两种方式指定 profile：

1. 在启动脚本前先设置环境变量

```bash
export HERMES_HOME=/home/iklare/.hermes/profiles/coder
```

2. 直接传 `--profile-home`

```bash
--profile-home /home/iklare/.hermes/profiles/coder
```

# 返回值

脚本只向标准输出写一段纯文本：

- 内容就是 `result.get("final_response", "")`

因此其他 Python 代码里可以直接用 `subprocess` 捕获 stdout，当作 Hermes AIAgent 的最终回复。

# 用法示例

```bash
python tmp/hermes_runner.py \
  --hermes-agent-dir /home/iklare/.hermes/hermes-agent \
  --profile-home /home/iklare/.hermes/profiles/coder \
  --workspace /path/to/your/workspace \
  --model ep-20260529115331-9zxpm \
  --base-url https://ark.cn-beijing.volces.com/api/v3 \
  --api-key xxx \
  --session-id 1145141919810 \
  --max-tokens 102400 \
  --user-message "你好呀"
```

# 在其他 Python 中调用

示例脚本 `tmp/call_hermes_runner.py` 使用普通的 `subprocess.run(...)`。
如果你在其他项目里本身是 async 代码，也可以改用 `asyncio.create_subprocess_exec(...)`
来启动 `tmp/hermes_runner.py`，用法是一样的。

```python
import subprocess

completed = subprocess.run(
    [
        "/home/iklare/.hermes/hermes-agent/.venv/bin/python",
        "/home/iklare/.hermes/hermes-agent/tmp/hermes_runner.py",
        "--hermes-agent-dir", "/home/iklare/.hermes/hermes-agent",
        "--profile-home", "/home/iklare/.hermes/profiles/coder",
        "--workspace", "/path/to/your/workspace",
        "--model", "ep-20260529115331-9zxpm",
        "--base-url", "https://ark.cn-beijing.volces.com/api/v3",
        "--api-key", "xxx",
        "--session-id", "1145141919810",
        "--max-tokens", "102400",
        "--user-message", "你好呀",
    ],
    cwd="/path/to/your/workspace",
    capture_output=True,
    text=True,
)
print(completed.stdout)
```

# 说明

- 脚本会等待 background review 线程结束后再退出
- 但返回给调用方的只有主对话回复，不包含 review 输出
- 默认会同时触发 `review_skills` 和 `review_memory`
- 如果你完全不想触发某一类 review，就传 `--no-review-skills` 或 `--no-review-memory`
- `call_hermes_runner.py` 使用的是普通 `subprocess.run(...)`
- 如果你的外部项目本身是 async，也可以用 `asyncio.create_subprocess_exec(...)` 启动 `hermes_runner.py`
