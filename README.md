# t32-mcp

面向 Windows 的 MCP 服务端。Agent 可以启动或连接 Lauterbach TRACE32 PowerView，执行调试命令、运行 PRACTICE 脚本并读取窗口输出。

## 能解决什么

- 命令batch和脚本可返回失败位置、PowerView 的诊断，以及从本机手册查到的语法提示。
- `view` 通过 PowerView 的打印接口导出窗口。超过回包长度时会给出本地结果路径。
- 长操作返回操作编号，供后续查询。同步调用超过期限时，服务可终止自己启动的 PowerView 并报告会话状态。

## 安装

下载源码，在项目目录运行，Python版本要求为3.12或更高。

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m t32_mcp --help
```

### 使用

在 MCP 客户端中添加 `t32` 的 stdio 服务。如果客户端使用 `mcpServers`，示例如下。见[配置模板](.mcp.json.example)。

```json
{
  "mcpServers": {
    "t32": {
      "command": "C:/path/to/t32-mcp/.venv/Scripts/python.exe",
      "args": ["-m", "t32_mcp", "--sys", "C:/T32"]
    }
  }
}
```

`command` 指向.venv安装的 `python.exe`。路径可在根目录执行指令获得。

```powershell
(Resolve-Path .\.venv\Scripts\python.exe).Path.Replace('\', '/')
```

`C:/T32` 改为实际的 TRACE32 安装目录，无参数默认值为 `C:\T32`，可由T32SYS环境变量覆盖。

## 工具

| 工具 | 用途 |
|---|---|
| `session_open` | 启动 PowerView，或Attach到当前已运行实例 |
| `session_status` | 版本、探头、License、目标状态、各目录路径 |
| `session_close` | 关闭，或把 PowerView 留给用户 |
| `command` | 执行 PRACTICE 命令 |
| `evaluate` | 批量求值 PRACTICE 函数 |
| `script` | 运行 `.cmm` 脚本 |
| `view` | 读 T32 窗口 |
| `run` | 运行控制，等到目标停下 |
| `manual` | 查手册：给名字返回语法，给几个词返回候选，不需要会话 |
| `operation_get` | 继续等待未结束的操作 |
| `operation_cancel` | 取消等待或中止操作 |

## 三种连接

- `usb`：在探头上启动一台 PowerView，窗口可见，你可以旁观。
- `sim`：启动指令集模拟器，有空闲探头时用探头授权。
- `attach`：连上你已经开着的 PowerView，默认只读。

Attach时如果 PowerView 没开 API，在命令行输入：

```text
SETUP.API.RCL.Enable /PRotocol TCP /Port AUTO /RemoteHost DENY
```

不用重启，也不影响已经连着的目标。端口由服务端自己查找。

## 运行记录

从源码安装时，会话文件放在本仓库的 `tmp/sessions/`。安装 wheel 时，目录在虚拟环境的 `t32_mcp/tmp/sessions/` 下。MCP 和 PowerView 都需要能读写这个位置。每个会话一个子目录，保留最近 20 个：

- `journal.jsonl` 记录操作开始、参数和已取得的结果。进程意外退出时，最后一次操作可能只有开始记录。
- `view-<op>.txt` 保存 PowerView 导出的窗口内容。
- `script-<op>.cmm`、`script-<op>.log`：脚本和它的输出。

## 测试

```powershell
.venv\Scripts\python -m pip install "pytest>=8"
.venv\Scripts\python -m pytest                       # 默认离线，包含模拟的板级与 Trace 场景
.venv\Scripts\python -m pytest -m hw                 # 运行需要 PowerView 和探头的集成测试
.venv\Scripts\python tools\mutation_check.py          # 离线变异检查
.venv\Scripts\python tools\mutation_check.py --live   # 也运行需要 PowerView 的变异检查
```

- 板级与 Trace 场景在 RAPI 边界提供模拟响应，用实际的服务代码验证运行控制、错误传播和结果交付。默认测试可在没有探头或实验固件的环境中运行。
- `hw` 集成测试用于配有一台空闲 PowerDebug 的独占测试台。它们会启动真实 PowerView，部分用例会终止测试进程并验证探头恢复。
- 检查特定版本的窗口导出时，使用自己的匹配 ELF 和 Trace 归档。运行 `python tools/window_sweep.py --help` 可查看参数。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。运行实机调试需要另行安装并授权 Lauterbach TRACE32。
