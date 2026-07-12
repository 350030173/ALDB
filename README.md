# ALDB - Android Logcat Display in Browser

安卓日志在浏览器中实时显示，底部集成 PTY 终端，支持 Frida 交互。

## 依赖项

| 依赖 | 类型 | 说明 |
|------|------|------|
| **Python 3.8+** | 运行环境 | [python.org](https://www.python.org/downloads/) 下载，安装时勾选 "Add Python to PATH" |
| **adb** | 外部工具 | Android SDK Platform Tools，[下载地址](https://developer.android.com/studio/releases/platform-tools)，解压后将目录加入系统 PATH |
| `websockets` | Python 库 | WebSocket 通信，双击 bat 自动安装 |
| `pywinpty` | Python 库（可选）| Windows 终端颜色/方向键支持，双击 bat 自动安装 |

## 使用方式

### 前提

1. 安装 **Python 3.8+**（勾选 Add to PATH）
2. 安装 **adb** 并加入 PATH（确保终端输入 `adb` 能识别）
3. 手机 **USB 连接**电脑，开启 **USB 调试**

### 使用

```
双击 logcat.bat
```

自动完成：检查 Python → 安装缺失的库 → 检测 adb 和设备 → 启动服务 → 打开浏览器。

### 手动安装依赖（如果 bat 自动安装失败）

```bash
pip install -r requirements.txt
```

### requirements.txt 内容

```
websockets>=13.0        # WebSocket 服务（必需）
pywinpty>=2.0           # Windows 终端 PTY 支持（可选，无此库也能用但终端功能受限）
```

## 功能

- **实时日志**：手机所有应用的 logcat 输出在浏览器中实时显示
- **过滤搜索**：按包名、Tag、文本、日志级别筛选
- **底部 PTY 终端**：直接输入 shell 命令，支持 Frida 等交互工具
- **命令历史**：PowerShell 自动保存历史（按 ↑ 键）
- **中英文切换**：工具栏右侧切换语言
- **设置持久化**：关掉网页重开，所有设置自动恢复

## 端口

| 端口 | 协议 | 用途 |
|------|------|------|
| 8765 | WebSocket | logcat 日志流 + 终端通信 |
| 8766 | HTTP | 清缓冲区、一次性命令执行 |
<img width="1259" height="671" alt="image" src="https://github.com/user-attachments/assets/03fc73be-f01b-4322-9a2d-af60d6eb4457" />
