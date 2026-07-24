import asyncio
import websockets
import json
import os
import sys
import threading
import queue
import time
import subprocess
import base64
import select as _select_mod
import re

# ========== ANSI 转义序列过滤 ==========
_ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\].*?\x07|\x1b\[.*?[a-zA-Z]')

def strip_ansi(text):
    """去除 ANSI 转义序列（颜色码、光标控制等），保留纯文本"""
    return _ANSI_RE.sub('', text)

# ========== 真正的 PTY 终端（支持 Frida 等交互程序）==========

class PtyTerminal:
    """跨平台伪终端（PTY）：给 Frida 等交互程序提供真正的 TTY

    依赖:
      Windows:  pip install pywinpty  （未安装则回退到 subprocess 管道）
      Linux/Mac: 使用标准库 pty 模块，无需额外安装
    """
    def __init__(self, session_id, cols=120, rows=30):
        self.id = session_id
        self.cols = cols
        self.rows = rows
        self.process = None       # PtyProcess (win) 或 subprocess.Popen (posix)
        self.master_fd = None     # posix: PTY master 文件描述符
        self._use_pty = False     # True = 真正的 PTY, False = subprocess 回退
        self._is_winpty = False   # True = pywinpty
        self.output_queue = queue.Queue()
        self.running = False
        self.closed = False
        self._reader_thread = None
        self._log_file = None     # 日志文件句柄
        self._log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'CommandLog')
        self._input_buf = ''      # 输入缓冲（攒到回车才写日志）

    # ---- 启动 ----

    def start(self, cmd=None):
        """启动终端进程，自动选择最佳后端"""
        self._open_log()
        if sys.platform == 'win32':
            return self._start_win32(cmd)
        else:
            return self._start_posix(cmd)

    def _start_win32(self, cmd):
        """Windows: 优先用 pywinpty (ConPTY), 未安装则回退 subprocess"""
        # 尝试加载 pywinpty
        try:
            from winpty import PtyProcess as WinPty
        except ImportError:
            print(f"[TERM {self.id}] pywinpty 未安装，回退到 subprocess 模式")
            print(f"[TERM {self.id}] 提示: pip install pywinpty 可启用完整终端支持（颜色/方向键/Frida交互）")
            return self._start_subprocess_win32(cmd)

        try:
            # 没有指定命令时用 PowerShell（自动保存历史到磁盘）
            # 指定命令时（如 frida）仍用 cmd.exe /k 包裹执行
            if cmd:
                spawn_args = f'cmd.exe /k "{cmd}"'
            else:
                spawn_args = 'powershell.exe -NoLogo'
            self.process = WinPty.spawn(spawn_args, dimensions=(self.rows, self.cols))
            self._use_pty = True
            self._is_winpty = True
            self.running = True
            self._reader_thread = threading.Thread(
                target=self._read_winpty, name=f"term-{self.id}", daemon=True
            )
            self._reader_thread.start()
            backend = 'winpty (ConPTY)'
            print(f"[TERM {self.id}] 启动 ({backend}): {cmd or 'PowerShell'} ({self.cols}x{self.rows})")
            return True
        except Exception as e:
            print(f"[TERM {self.id}] winpty 启动失败: {e}，回退到 subprocess")
            return self._start_subprocess_win32(cmd)

    def _start_subprocess_win32(self, cmd):
        """Windows 回退方案: 使用 subprocess 管道"""
        self._use_pty = False
        try:
            if cmd:
                full_cmd = f'cmd.exe /k "{cmd}"'
                self.process = subprocess.Popen(
                    full_cmd,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=False, bufsize=0,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                self.process = subprocess.Popen(
                    'powershell.exe -NoLogo',
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=False, bufsize=0,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            self.running = True
            self._reader_thread = threading.Thread(
                target=self._read_subprocess, name=f"term-{self.id}", daemon=True
            )
            self._reader_thread.start()
            print(f"[TERM {self.id}] 启动 (subprocess): {cmd or 'PowerShell'}")
            return True
        except Exception as e:
            print(f"[TERM {self.id}] subprocess 启动失败: {e}")
            return False

    def _start_posix(self, cmd):
        """Linux/Mac: 使用标准库 pty + subprocess"""
        import pty as _pty
        import termios as _termios
        import struct as _struct
        import fcntl as _fcntl

        try:
            master_fd, slave_fd = _pty.openpty()
            # 设置初始窗口大小
            winsize = _struct.pack("HHHH", self.rows, self.cols, 0, 0)
            _fcntl.ioctl(slave_fd, _termios.TIOCSWINSZ, winsize)

            if cmd:
                self.process = subprocess.Popen(
                    cmd, shell=True,
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    preexec_fn=os.setsid,
                )
            else:
                self.process = subprocess.Popen(
                    ['/bin/bash'],
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    preexec_fn=os.setsid,
                )
            os.close(slave_fd)
            self.master_fd = master_fd

            # 设置 master_fd 为非阻塞
            fl = _fcntl.fcntl(master_fd, _fcntl.F_GETFL)
            _fcntl.fcntl(master_fd, _fcntl.F_SETFL, fl | os.O_NONBLOCK)

            self._use_pty = True
            self.running = True
            self._reader_thread = threading.Thread(
                target=self._read_posix_pty, name=f"term-{self.id}", daemon=True
            )
            self._reader_thread.start()
            print(f"[TERM {self.id}] 启动 (posix pty): {cmd or 'bash'} ({self.cols}x{self.rows})")
            return True
        except Exception as e:
            print(f"[TERM {self.id}] posix pty 启动失败: {e}")
            return False

    # ---- 后台读取线程 ----

    def _read_winpty(self):
        """pywinpty 后台读取（返回 str，含完整 ANSI 转义序列）"""
        try:
            while self.running and self.process and self.process.isalive():
                try:
                    data = self.process.read(4096)
                    if data:
                        self._log_output(data)
                        self.output_queue.put(data)  # str
                except Exception:
                    if not self.process.isalive():
                        break
                    time.sleep(0.005)
        except Exception as e:
            print(f"[TERM {self.id}] winpty 读取错误: {e}")
        finally:
            self.output_queue.put(None)
            print(f"[TERM {self.id}] winpty 读取线程退出")

    def _read_posix_pty(self):
        """posix PTY 后台读取（返回 bytes）"""
        try:
            while self.running and self.process and self.process.poll() is None:
                try:
                    r, _, _ = _select_mod.select([self.master_fd], [], [], 0.05)
                    if r:
                        data = os.read(self.master_fd, 4096)
                        if data:
                            self._log_output(data)
                            self.output_queue.put(data)  # bytes
                        else:
                            break
                except Exception:
                    if self.process.poll() is not None:
                        break
                    time.sleep(0.01)
            # 排空残余数据
            self._drain_fd()
        except Exception as e:
            print(f"[TERM {self.id}] posix pty 读取错误: {e}")
        finally:
            self.output_queue.put(None)
            print(f"[TERM {self.id}] posix pty 读取线程退出")

    def _read_subprocess(self):
        """subprocess 管道回退读取（同旧版 SimpleTerminal 逻辑）"""
        try:
            while self.running and self.process and self.process.stdout:
                try:
                    data = self.process.stdout.read(1)
                    if not data:
                        if self.process.poll() is not None:
                            break
                        time.sleep(0.01)
                        continue
                    # 批量读取
                    if hasattr(_select_mod, 'select'):
                        while True:
                            ready, _, _ = _select_mod.select(
                                [self.process.stdout], [], [], 0.01
                            )
                            if not ready:
                                break
                            chunk = self.process.stdout.read(4095)
                            if chunk:
                                data += chunk
                            else:
                                break
                    text = _decode_text(data)
                    if text:
                        self._log_output(text)
                        self.output_queue.put(text)
                except Exception:
                    if self.process.poll() is not None:
                        break
                    time.sleep(0.01)
        except Exception as e:
            print(f"[TERM {self.id}] subprocess 读取错误: {e}")
        finally:
            self.output_queue.put(None)

    def _drain_fd(self):
        """排空文件描述符中的残余数据"""
        try:
            for _ in range(20):
                r, _, _ = _select_mod.select([self.master_fd], [], [], 0.05)
                if r:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        self._log_output(data)
                        self.output_queue.put(data)
                    else:
                        break
                else:
                    break
        except Exception:
            pass

    # ---- 输入 ----

    def write(self, data):
        """向终端写入数据（键盘输入）"""
        # 日志记录：缓冲到回车再写入
        self._log_input(data)

        try:
            if self._is_winpty:
                self.process.write(data)
            elif self._use_pty and self.master_fd is not None:
                if isinstance(data, str):
                    data = data.encode('utf-8')
                os.write(self.master_fd, data)
            elif self.process and self.process.stdin:
                if isinstance(data, str):
                    data = data.encode('utf-8')
                self.process.stdin.write(data)
                self.process.stdin.flush()
            else:
                return False
            return True
        except Exception as e:
            print(f"[TERM {self.id}] 写入失败: {e}")
            return False

    # ---- resize ----

    def resize(self, cols, rows):
        self.cols = cols
        self.rows = rows
        try:
            if self._is_winpty:
                self.process.setwinsize(rows, cols)
            elif self.master_fd is not None:
                import termios as _termios
                import struct as _struct
                import fcntl as _fcntl
                winsize = _struct.pack("HHHH", rows, cols, 0, 0)
                _fcntl.ioctl(self.master_fd, _termios.TIOCSWINSZ, winsize)
        except Exception as e:
            pass  # resize 不是关键功能，静默失败

    # ---- 读取（供 asyncio 轮询） ----

    def read(self, timeout=0.1):
        """读取终端输出，返回 list: [str|bytes, ...]"""
        outputs = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                data = self.output_queue.get(timeout=min(remaining, 0.02))
                if data is None:
                    break
                outputs.append(data)
            except queue.Empty:
                break
        return outputs

    def is_running(self):
        if self._is_winpty:
            return self.running and self.process and self.process.isalive()
        if self._use_pty:
            return self.running and self.process and self.process.poll() is None
        return self.running and self.process and self.process.poll() is None

    # ---- 日志记录 ----

    def _open_log(self):
        """创建日志文件"""
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            ts = time.strftime('%Y-%m-%d_%H-%M-%S')
            fname = f'{self.id}_{ts}.txt'
            self._log_file = open(os.path.join(self._log_dir, fname), 'w', encoding='utf-8')
            self._log_file.write(f'=== Terminal Log: {self.id} ===\n')
            self._log_file.write(f'Started: {time.strftime("%Y-%m-%d %H:%M:%S")}\n\n')
            self._log_file.flush()
        except Exception as e:
            print(f"[TERM {self.id}] 无法创建日志文件: {e}")
            self._log_file = None

    def _log_input(self, data):
        """缓冲输入，遇到回车时写入日志"""
        if not self._log_file or self.closed:
            return
        try:
            for ch in data:
                if ch == '\r' or ch == '\n':
                    if self._input_buf.strip():
                        ts = time.strftime('%H:%M:%S')
                        self._log_file.write(f'[{ts}] > {self._input_buf}\n')
                        self._log_file.flush()
                    self._input_buf = ''
                elif ch == '\x7f' or ord(ch) == 8:  # Backspace
                    self._input_buf = self._input_buf[:-1]
                elif ord(ch) >= 32:  # 可打印字符
                    self._input_buf += ch
        except Exception:
            pass

    def _log_output(self, data):
        """记录终端输出到日志（去除 ANSI 转义序列和多余空行）"""
        if not self._log_file or self.closed:
            return
        try:
            if isinstance(data, bytes):
                text = data.decode('utf-8', errors='replace')
            else:
                text = data
            if text:
                clean = strip_ansi(text)
                # 去掉连续空行，只保留单个换行
                clean = re.sub(r'\n\s*\n', '\n', clean)
                # 去掉行尾的 \r
                clean = clean.replace('\r', '')
                if clean.strip():
                    self._log_file.write(clean)
                    self._log_file.flush()
        except Exception:
            pass

    def close(self):
        """关闭终端"""
        self.running = False
        self.closed = True
        # 关闭日志文件
        if self._log_file:
            try:
                if self._input_buf.strip():
                    self._log_file.write(f'[---] > {self._input_buf} (未完成)\n')
                self._log_file.write(f'\n=== Closed: {time.strftime("%Y-%m-%d %H:%M:%S")} ===\n')
                self._log_file.close()
                self._log_file = None
            except Exception:
                pass
        try:
            if self.process:
                self.process.terminate()
                time.sleep(0.1)
                try:
                    if self._is_winpty:
                        pass  # terminate 已足够
                    elif self.process.poll() is None:
                        self.process.kill()
                except Exception:
                    pass
                self.process = None
        except Exception:
            pass
        try:
            if self.master_fd is not None:
                os.close(self.master_fd)
                self.master_fd = None
        except Exception:
            pass
        print(f"[TERM {self.id}] 已关闭")


def _decode_text(data):
    """解码 subprocess 回退模式下的字节数据"""
    if not data:
        return ''
    for enc in ('utf-8', 'gbk', 'gb2312', 'gb18030', 'cp936', 'latin-1'):
        try:
            return data.decode(enc, errors='strict')
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode('utf-8', errors='replace')


# ========== 全局存储 ==========
active_terminals = {}

# ========== WebSocket 处理器 ==========

async def terminal_handler(websocket, path):
    """处理终端 WebSocket 连接（支持多 Tab）"""
    my_sessions = set()  # 追踪本连接创建的所有终端

    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get('type')

            if msg_type == 'create':
                session_id = data.get('session_id', str(id(websocket)))
                cols = data.get('cols', 120)
                rows = data.get('rows', 30)
                cmd = data.get('command')

                try:
                    term = PtyTerminal(session_id, cols, rows)
                    if term.start(cmd):
                        active_terminals[session_id] = term
                        my_sessions.add(session_id)
                        asyncio.create_task(push_output(websocket, term))
                        await websocket.send(json.dumps({
                            'type': 'created',
                            'session_id': session_id
                        }))
                    else:
                        await websocket.send(json.dumps({
                            'type': 'error',
                            'data': '终端启动失败'
                        }))
                except Exception as e:
                    await websocket.send(json.dumps({
                        'type': 'error',
                        'data': str(e)
                    }))

            elif msg_type == 'input':
                session_id = data.get('session_id')
                if session_id in active_terminals:
                    input_data = data.get('data', '')
                    active_terminals[session_id].write(input_data)

            elif msg_type == 'resize':
                session_id = data.get('session_id')
                if session_id in active_terminals:
                    cols = data.get('cols', 120)
                    rows = data.get('rows', 30)
                    active_terminals[session_id].resize(cols, rows)

            elif msg_type == 'close':
                session_id = data.get('session_id')
                if session_id in active_terminals:
                    active_terminals[session_id].close()
                    del active_terminals[session_id]
                    my_sessions.discard(session_id)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        for sid in list(my_sessions):
            if sid in active_terminals:
                active_terminals[sid].close()
                del active_terminals[sid]

async def push_output(websocket, term):
    """持续推送终端输出到浏览器（兼容 str 和 bytes）"""
    try:
        while term.is_running() and not term.closed:
            parts = term.read(timeout=0.05)
            for part in parts:
                if isinstance(part, bytes):
                    # posix PTY 输出是 bytes（含 ANSI 转义序列）
                    # 用 base64 编码，客户端解码为 Uint8Array 后传给 xterm.js
                    b64 = base64.b64encode(part).decode('ascii')
                    await websocket.send(json.dumps({
                        'type': 'output',
                        'data': b64,
                        'binary': True
                    }))
                elif part:
                    # pywinpty 或 subprocess 回退输出（已是 str）
                    await websocket.send(json.dumps({
                        'type': 'output',
                        'data': part
                    }, ensure_ascii=False))
            await asyncio.sleep(0.01)
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"[TERM {term.id}] 推送错误: {e}")


# ========== 日志 WebSocket ==========

async def logcat_handler(websocket, path):
    """处理 logcat WebSocket 连接（异步非阻塞版）

    关键改进：
    1. 后台线程读取 subprocess stdout，避免阻塞 asyncio 事件循环
    2. 使用 adb shell logcat 让设备端 shell 以行缓冲模式输出
    3. 通过 asyncio.Queue + call_soon_threadsafe 实现线程安全通信
    4. 二进制模式读取 + 手动按行拆分，避免 TextIOWrapper 的额外缓冲
    """
    print("[LOGCAT] 清除手机日志缓冲区...")
    try:
        clear_proc = await asyncio.create_subprocess_exec(
            "adb", "logcat", "-c",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await clear_proc.wait()
        print("[LOGCAT] 缓冲区已清除")
    except FileNotFoundError:
        print("[LOGCAT] 错误: 找不到 adb 命令，请确认 Android SDK 已安装并配置 PATH")
        await websocket.send("❌ 错误: 找不到 adb 命令，请确认 Android SDK 已安装")
        return
    except Exception as e:
        print(f"[LOGCAT] 清除缓冲区失败: {e}")

    # 启动 adb shell logcat（设备端 shell 的行缓冲更友好）
    proc = subprocess.Popen(
        # 使用 adb shell 让 logcat 在设备端运行
        # 设备端 shell 连接 tty 时通常使用行缓冲，输出更实时
        ["adb", "shell", "logcat", "-v", "threadtime"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=False,   # 二进制模式，避免 TextIOWrapper 额外缓冲
        bufsize=0,    # Python 侧无缓冲
    )

    print(f"[LOGCAT] 已启动 adb shell logcat (PID={proc.pid})")

    # asyncio 安全队列：reader 线程写入，asyncio 协程读取
    loop = asyncio.get_event_loop()
    log_queue = asyncio.Queue()  # 无界队列，避免背压时丢日志

    def reader_thread():
        """后台线程：持续读取 adb stdout，按行拆分后推送到 asyncio 队列"""
        buffer = b""
        try:
            while proc.poll() is None:
                try:
                    # 读取最多 64KB，减少系统调用开销
                    data = proc.stdout.read(65536)
                    if data:
                        buffer += data
                        # 按换行拆分，每拆分出一行就立即推送
                        while True:
                            idx = buffer.find(b"\n")
                            if idx == -1:
                                break
                            line_bytes = buffer[:idx]
                            buffer = buffer[idx + 1:]
                            if line_bytes:
                                # 跳过空行，解码后推送
                                text = _decode_line(line_bytes)
                                if text is not None:
                                    loop.call_soon_threadsafe(
                                        log_queue.put_nowait, text
                                    )
                    elif proc.poll() is not None:
                        # stdout 已关闭且进程已退出
                        break
                    else:
                        # 短暂等待后重试
                        time.sleep(0.005)
                except Exception:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.01)

            # 刷新缓冲区残余数据
            if buffer:
                text = _decode_line(buffer)
                if text is not None:
                    loop.call_soon_threadsafe(log_queue.put_nowait, text)

        except Exception as e:
            print(f"[LOGCAT] 读取线程异常: {e}")
        finally:
            # 发送结束标记
            loop.call_soon_threadsafe(log_queue.put_nowait, None)
            print("[LOGCAT] 读取线程已退出")

    def _decode_line(data):
        """解码一行字节数据，尝试多种编码"""
        if not data:
            return None
        # 去除尾部的 \r
        data = data.rstrip(b"\r")
        if not data:
            return None
        for encoding in ("utf-8", "gbk", "gb2312", "gb18030", "cp936", "latin-1"):
            try:
                return data.decode(encoding, errors="strict")
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("utf-8", errors="replace")

    reader = threading.Thread(target=reader_thread, name="logcat-reader", daemon=True)
    reader.start()

    try:
        while True:
            line = await log_queue.get()
            if line is None:
                # 读取线程已结束
                break
            try:
                await websocket.send(line)
            except websockets.exceptions.ConnectionClosed:
                break
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"[LOGCAT] 发送异常: {e}")
    finally:
        print("[LOGCAT] 正在清理...")
        # 先等待队列清空（最多 1 秒）
        remaining = 0
        while not log_queue.empty() and remaining < 100:
            try:
                log_queue.get_nowait()
                remaining += 1
            except asyncio.QueueEmpty:
                break
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
        print(f"[LOGCAT] 已清理 (PID={proc.pid})")


# ========== HTTP 服务 ==========

import http.server

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logcat_settings.json')

# ========== Logcat 日志保存开关 ==========
LOGCAT_LOG_ENABLED = False
LOGCAT_LOG_FILE = None
LOGCAT_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logcat')

def _open_logcat_log():
    global LOGCAT_LOG_FILE
    try:
        os.makedirs(LOGCAT_LOG_DIR, exist_ok=True)
        ts = time.strftime('%Y-%m-%d_%H-%M-%S')
        path = os.path.join(LOGCAT_LOG_DIR, f'logcat_{ts}.txt')
        LOGCAT_LOG_FILE = open(path, 'w', encoding='utf-8')
        print(f"[LOGCAT] 日志保存开启: {path}")
    except Exception as e:
        print(f"[LOGCAT] 无法创建日志文件: {e}")

def _close_logcat_log():
    global LOGCAT_LOG_FILE
    if LOGCAT_LOG_FILE:
        LOGCAT_LOG_FILE.close()
        LOGCAT_LOG_FILE = None

class HTTPHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path.startswith('/logcat-toggle'):
            global LOGCAT_LOG_ENABLED
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            enable = qs.get('enable', ['0'])[0] == '1'
            LOGCAT_LOG_ENABLED = enable
            if enable:
                _open_logcat_log()
            else:
                _close_logcat_log()
            self._send_json(200, {'success': True, 'enabled': LOGCAT_LOG_ENABLED})
        elif self.path == '/settings':
            try:
                if os.path.exists(SETTINGS_FILE):
                    with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self._send_json(200, data)
                else:
                    self._send_json(200, {})
            except Exception as e:
                self._send_json(500, {'error': str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        if self.path == '/clear':
            subprocess.run(["adb", "logcat", "-c"], capture_output=True, text=True)
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'{"success": true}')

        elif self.path == '/logcat-write':
            try:
                if LOGCAT_LOG_ENABLED and LOGCAT_LOG_FILE:
                    lines = body.split('\n')
                    for line in lines:
                        LOGCAT_LOG_FILE.write(line + '\n')
                    LOGCAT_LOG_FILE.flush()
                    self._send_json(200, {'success': True, 'count': len(lines)})
                else:
                    self._send_json(200, {'success': False, 'reason': 'not enabled'})
            except Exception as e:
                self._send_json(500, {'error': str(e)})

        elif self.path == '/settings':
            try:
                data = json.loads(body)
                # 过滤掉空值，避免污染文件
                clean = {k: v for k, v in data.items() if v is not None and v != ''}
                with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(clean, f, ensure_ascii=False, indent=2)
                self._send_json(200, {'success': True})
            except Exception as e:
                self._send_json(500, {'error': str(e)})

        # 一次性命令执行
        elif self.path == '/cmd':
            try:
                data = json.loads(body)
                cmd = data.get('command', '').strip()
                if not cmd:
                    self._send_json(400, {'success': False, 'error': '命令为空'})
                    return

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=60,
                    shell=True
                )
                self._send_json(200, {
                    'success': True,
                    'stdout': result.stdout,
                    'stderr': result.stderr,
                    'running': False,
                    'returncode': result.returncode
                })
            except Exception as e:
                self._send_json(500, {'success': False, 'error': str(e)})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _send_json(self, status, data):
        self.send_response(status)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args):
        pass

def start_http_server():
    server = http.server.HTTPServer(('localhost', 8766), HTTPHandler)
    print("✅ HTTP server started at http://localhost:8766")
    server.serve_forever()


# ========== 主程序 ==========

async def main():
    threading.Thread(target=start_http_server, daemon=True).start()

    async def route_handler(websocket):
        path = websocket.request.path   # 获取请求路径
        if path == '/terminal':
            await terminal_handler(websocket, path)
        else:
            await logcat_handler(websocket, path)

    server = await websockets.serve(route_handler, "localhost", 8765)
    print("✅ WebSocket server started at ws://localhost:8765")
    print("  - ws://localhost:8765/terminal  交互式终端 (PTY)")
    print("  - ws://localhost:8765/          logcat 连接")
    print("📱 打开 logcat_terminal.html 查看日志和终端")
    print("💡 Windows 用户: pip install pywinpty 启用完整终端支持 (颜色/方向键/Frida)")
    await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
