# OpenHuman workspace seed

OpenHuman baseline 不需要 OpenClaw 那套 IDENTITY / SOUL / USER / HEARTBEAT 文件，
因此本目录默认仅保留此 README，作为占位让 `COPY workspace_seed /opt/lift/workspace_seed`
不至于失败。

LIFT 在 holdout work/judge 容器对启动时仍会调用 `seed_workspace=True`，把目录内容复制到
`/workspace/task`；后续若有 OpenHuman 特有的 task 提示模板需要预置，直接放到本
目录即可，沿用与 GenericAgent / OpenClaw 一致的约定。
