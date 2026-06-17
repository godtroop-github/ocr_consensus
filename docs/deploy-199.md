# 199 远程部署固化说明

## 一键部署

```bash
bash ops/deploy_199.sh
```

默认部署到：

```text
http://128.18.185.199:8090/
```

## 固定约定

- 本地打包路径必须保留 `ops/...` 目录结构。
- 远端解压目录固定为 `/home/gt/ocr-fx/ocr_system`。
- 远端解压时不要使用 `--strip-components`。
- 远端 Web 服务必须使用 `/home/gt/ocr-fx/venv/bin/python` 启动。
- 不要使用系统 `python3`，否则会缺 `uvicorn`。
- 上传包时先执行 `gw_env_check`，再用 `gw_ssh "cat > remote" < package`。
- 不要让 `gw_ssh` 在 stdin 重定向时再次做链路检测，否则可能消耗 tar 输入流，导致远端 `gzip: stdin: unexpected end of file`。
- 重启时不要使用 `pkill -f consensus_task_app_server.py`，容易杀到当前远程 shell。
- 重启应按端口 `8090` 找监听进程并 kill。

## 可覆盖变量

```bash
REMOTE_ROOT=/home/gt/ocr-fx/ocr_system \
REMOTE_PYTHON=/home/gt/ocr-fx/venv/bin/python \
REMOTE_PORT=8090 \
bash ops/deploy_199.sh
```

## 这套逻辑解决过的问题

- 旧问题：解压用了 `--strip-components=2`，页面覆盖到错误目录，199 仍显示旧页面。
- 旧问题：用系统 `python3` 启动，报 `ModuleNotFoundError: No module named 'uvicorn'`。
- 旧问题：`gw_ssh 'cat > remote' < package` 前未固定 `GW_MODE`，链路检测消耗 stdin，导致 tar 包损坏。
- 旧问题：`pkill -f` 杀到了当前远程命令，SSH 返回 `255`。

