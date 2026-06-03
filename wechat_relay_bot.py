"""
微信自动接龙脚本 v3.0
========================
功能：监控微信群聊中的接龙消息，自动点击"参与接龙"并填写内容
原理：uiautomation 控件操作（适配微信 3.x）

使用方法：
  1. pip install uiautomation
  2. 修改 config.json
  3. 保持微信已登录，窗口打开，目标群聊在聊天列表可见
  4. 运行：python wechat_relay_bot.py
"""

import uiautomation as auto
import pyautogui
import json
import time
import logging
import os
import hashlib
import sys
import traceback
import subprocess
import ctypes
from typing import Optional, Set, List, Tuple

# ===== 路径 =====
BASE_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
PROCESSED_FILE = os.path.join(BASE_DIR, "processed_relays.json")
LOG_FILE = os.path.join(BASE_DIR, "relay_bot.log")

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.01

# ===== 默认配置 =====
DEFAULT_CONFIG = {
    "check_interval": 3.0,
    "response_text": "跟一个",
    "target_groups": [],
    "switch_group_interval": 10,
    "debug_mode": False,
}

# WeChat control names (Unicode)
WECHAT_WIN_NAME = "微信"
CHAT_LIST_NAME = "会话"
MSG_LIST_NAME = "消息"
RELAY_BTN_NAME = "参与接龙"


def load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return default if default is not None else {}


def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def setup_logging(debug=False):
    """Set up logging - only our module's messages, not uiautomation internals"""
    level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger()  # Root logger
    logger.setLevel(level)

    # Remove any existing handlers
    for h in logger.handlers[:]:
        logger.removeHandler(h)

    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )

    fh = logging.FileHandler(LOG_FILE, encoding='utf-8', mode='a')
    fh.setLevel(level)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setLevel(level)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    # Suppress uiautomation's verbose COM debug messages
    class UIAFilter(logging.Filter):
        def filter(self, record):
            msg = record.getMessage()
            return not ('Release <POINTER' in msg or 'POINTER(IUIAutomation' in msg
                        or 'GetModule' in msg or 'CoCreateInstance' in msg)

    for h in logger.handlers:
        if debug:
            h.addFilter(UIAFilter())
        else:
            # In non-debug mode, also filter out DEBUG level entirely
            if level > logging.DEBUG:
                pass  # Already filtered by level


class RelayBot:
    def __init__(self):
        self.config = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}
        self.processed: Set[str] = set(load_json(PROCESSED_FILE, []))
        self.window: Optional[auto.WindowControl] = None
        self._joining = False
        self._joining_timeout = 0
        self._last_group_switch = 0
        self._current_group_index = -1

    # ---------- 持久化 ----------

    def _save_processed(self):
        save_json(PROCESSED_FILE, list(self.processed))

    # ---------- 窗口保活 ----------

    def _ensure_window(self):
        """Refresh window reference if needed"""
        if not self.window or not self.window.Exists():
            win = auto.WindowControl(searchDepth=1, Name=WECHAT_WIN_NAME)
            if win.Exists():
                self.window = win
                return True
            return False
        return True

    # ---------- 窗口连接 ----------

    def _restore_wechat_window(self):
        """Try to restore WeChat window from tray off-screen state using Win32 API."""
        try:
            hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                time.sleep(0.3)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                return True
        except:
            pass
        return False

    def connect(self) -> bool:
        logging.info("正在连接微信...")
        # First try to restore the window if it's hidden
        self._restore_wechat_window()
        time.sleep(1)
        for i in range(30):
            try:
                win = auto.WindowControl(searchDepth=1, Name=WECHAT_WIN_NAME)
                if win.Exists():
                    self.window = win
                    win.SetActive()
                    time.sleep(0.5)
                    logging.info("已连接微信窗口")
                    return True
            except:
                pass
            if i == 0:
                logging.info("等待微信窗口...")
            time.sleep(1)
        logging.error("未找到微信窗口")
        return False

    # ---------- 控件搜索 ----------

    def _get_cols(self):
        """Get the 3-column layout of the WeChat window"""
        main = self.window.GetFirstChildControl()
        inner = main.GetFirstChildControl()
        return inner.GetChildren()

    def _find_list_in_panes(self, root, target_name, max_depth=8):
        """BFS through PaneControls only, looking for a named ListControl.
        This is fast because it skips non-container controls."""
        candidates = [root]
        for _ in range(max_depth):
            next_level = []
            for ctrl in candidates:
                try:
                    for child in ctrl.GetChildren():
                        if child.ControlTypeName == "ListControl":
                            if child.Name == target_name:
                                return child
                        elif child.ControlTypeName == "PaneControl":
                            next_level.append(child)
                except:
                    pass
            candidates = next_level
            if not candidates:
                break
        return None

    def _find_list_recursive(self, root, target_name, max_depth=10):
        """Recursive search through ALL controls for a named ListControl."""
        def dfs(ctrl, depth=0):
            if depth > max_depth:
                return None
            try:
                for child in ctrl.GetChildren():
                    if child.ControlTypeName == "ListControl" and child.Name == target_name:
                        return child
                    result = dfs(child, depth + 1)
                    if result:
                        return result
            except:
                pass
            return None
        return dfs(root)

    def _get_chat_list(self):
        """Get the chat session ListControl from the middle column (always visible)"""
        try:
            cols = self._get_cols()
            if len(cols) < 2:
                return None
            result = self._find_list_in_panes(cols[1], CHAT_LIST_NAME, 6)
            # If not found in middle column, try on the whole window
            if not result:
                result = self._find_list_in_panes(self.window, CHAT_LIST_NAME, 10)
            return result
        except:
            return None

    def _debug_list_chats(self):
        """Debug: dump all chat list items to understand structure."""
        try:
            cols = self._get_cols()
            if len(cols) >= 2:
                panes = []
                def collect(ctrl, depth=0):
                    if depth > 8:
                        return
                    try:
                        for child in ctrl.GetChildren():
                            if child.ControlTypeName in ("PaneControl", "ListControl"):
                                panes.append((depth, child.ControlTypeName, child.Name))
                                collect(child, depth + 1)
                    except:
                        pass
                collect(cols[1])
                for d, t, n in panes:
                    logging.debug(f"  [{d}] {t}: '{n}'")
        except:
            pass

    def _get_msg_list(self):
        """Get the message area ListControl - try right column, then full-window recursive search"""
        try:
            cols = self._get_cols()
            if len(cols) >= 3:
                result = self._find_list_in_panes(cols[2], MSG_LIST_NAME, 8)
                if result:
                    return result
        except:
            pass
        # Fallback: recursive search through ALL control types (handles layout edge cases)
        return self._find_list_recursive(self.window, MSG_LIST_NAME, 12)

    # ===== 剪贴板辅助 =====

    @staticmethod
    def _set_clipboard(text: str):
        """Set Windows clipboard to Unicode text using Win32 API directly (no pyperclip needed)."""
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 2
        # Fix ctypes types for 64-bit handle compatibility (handle may exceed 2^31)
        ctypes.windll.kernel32.GlobalAlloc.restype = ctypes.c_void_p
        ctypes.windll.kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        ctypes.windll.kernel32.GlobalLock.restype = ctypes.c_void_p
        ctypes.windll.kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        ctypes.windll.user32.SetClipboardData.restype = ctypes.c_void_p
        ctypes.windll.user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        bytes_data = (text + '\0').encode('utf-16-le')
        handle = ctypes.windll.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(bytes_data))
        ptr = ctypes.windll.kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, bytes_data, len(bytes_data))
        ctypes.windll.kernel32.GlobalUnlock(handle)
        ctypes.windll.user32.OpenClipboard(None)
        ctypes.windll.user32.EmptyClipboard()
        ctypes.windll.user32.SetClipboardData(CF_UNICODETEXT, handle)
        ctypes.windll.user32.CloseClipboard()

    # ---------- 群聊切换 ----------

    @staticmethod
    def _bring_window_to_front():
        """Bring WeChat window to front using Win32 API (fast, no COM timeout)."""
        try:
            hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                return True
        except:
            pass
        return False

    def _switch_to_group(self, name: str) -> bool:
        """Switch to a group in the chat list"""
        t0 = time.time()
        logging.debug(f"_switch_to_group({name})")
        self._ensure_window()

        # Bring window to front
        if not self._bring_window_to_front():
            self.window.SetActive()
        logging.info(f"  计时: 激活窗口={time.time()-t0:.2f}s")
        time.sleep(1.0)

        # Get chat list and search its visible items
        found = None
        try:
            chat_list = self._get_chat_list()
            if chat_list:
                for child in chat_list.GetChildren():
                    if child.ControlTypeName != "ListItemControl":
                        continue
                    if child.Name == name:
                        found = child
                        break
        except:
            pass
        logging.info(f"  计时: 聊天列表搜索={time.time()-t0:.2f}s")

        if not found:
            logging.warning(f"群聊「{name}」未找到，尝试 Ctrl+F")
            return self._switch_by_search(name)

        # ButtonControl found → use its parent ListItemControl
        if found.ControlTypeName == "ButtonControl":
            try:
                p = found.GetParentControl()
                if p and p.ControlTypeName == "ListItemControl":
                    found = p
            except:
                pass

        try:
            # Check if the item has a valid bounding rect (visible on screen)
            rect = found.BoundingRectangle
            try:
                if hasattr(rect, 'width'):
                    visible = rect.width > 0 and rect.height > 0
                else:
                    visible = rect[2] > rect[0] and rect[3] > rect[1]
            except:
                visible = False

            if not visible:
                logging.debug("列表项不可见，尝试 SetFocus + 重试...")
                try:
                    found.SetFocus()
                    time.sleep(0.5)
                except:
                    pass

                # Final visibility check
                try:
                    rect = found.BoundingRectangle
                    if hasattr(rect, 'width'):
                        visible = rect.width > 0 and rect.height > 0
                    else:
                        visible = rect[2] > rect[0] and rect[3] > rect[1]
                except:
                    visible = True

            if not visible:
                logging.warning("群聊项不可见，尝试 Ctrl+F 搜索切换")
                return self._switch_by_search(name)

            found.Click()
            # 轮询等待消息列表可用（最多约 5s）
            for _ in range(10):
                if self._get_msg_list():
                    break
                time.sleep(0.5)
            logging.info(f"已切换到群聊「{name}」")
            return True
        except Exception as e:
            logging.error(f"点击群聊「{name}」失败: {e}")
            return False

    def _switch_by_search(self, name: str) -> bool:
        """Fallback: use Ctrl+F, paste from clipboard, then Enter"""
        try:
            # Ensure window is focused (Win32, fast)
            self._bring_window_to_front()
            time.sleep(0.3)
            auto.SendKeys('{Ctrl}f')
            time.sleep(0.8)
            self._set_clipboard(name)
            time.sleep(0.2)
            auto.SendKeys('{Ctrl}v')
            time.sleep(0.5)
            auto.SendKeys('{Enter}')
            time.sleep(1.5)
            logging.info(f"通过搜索切换到「{name}」")
            return True
        except Exception as e:
            logging.error(f"搜索群聊「{name}」失败: {e}")
            return False

    def _switch_to_next_group(self):
        groups = self.config.get("target_groups", [])
        if not groups:
            return

        self._current_group_index = (self._current_group_index + 1) % len(groups)
        name = groups[self._current_group_index]
        logging.info(f"切换到群聊: {name}")

        for attempt in range(2):
            if self._switch_to_group(name):
                # After switching, wait and scan once to catch any existing relay
                time.sleep(0.5)
                return
            logging.warning(f"第 {attempt + 1} 次切换失败，重试...")
            time.sleep(3)

        logging.error(f"群聊「{name}」切换失败，跳过")

    # ---------- 滚动 ----------

    def _scroll_msg_list(self):
        """Try to scroll the message list to reveal latest messages"""
        msg_list = self._get_msg_list()
        if msg_list:
            try:
                scroll = msg_list.GetScrollPattern()
                if scroll:
                    scroll.SetScrollPercent(100, 100)  # Scroll to bottom
                    time.sleep(0.05)
                    return
            except:
                pass
        # Fallback: keyboard
        try:
            auto.SendKeys('{End}')
            time.sleep(0.3)
        except:
            pass

    # ---------- 接龙检测 ----------

    @staticmethod
    def _relay_key_text(relay_text: str) -> str:
        """Extract first 3 lines of relay for dedup hashing."""
        lines = relay_text.split('\n')
        return '\n'.join(lines[:3])

    def _relay_hash(self, relay_text: str) -> str:
        return hashlib.sha256(self._relay_key_text(relay_text).encode('utf-8')).hexdigest()[:16]

    def _is_new_relay(self, relay_text: str) -> bool:
        return self._relay_hash(relay_text) not in self.processed

    def _mark_relay_done(self, relay_text: str):
        self.processed.add(self._relay_hash(relay_text))
        self._save_processed()

    def _is_relay_item(self, item) -> bool:
        """Check if a ListItemControl represents a relay message."""
        try:
            name = item.Name
            return bool(name and ('接龙' in name or name.startswith('#')))
        except:
            return False

    def _find_button_in_pane(self, root) -> Optional[any]:
        """Find the relay join button within a container.
        The button has InvokePattern available and typically has an empty Name."""
        def find_relay_btn(ctrl, depth=0):
            if depth > 10:
                return None
            try:
                for child in ctrl.GetChildren():
                    ct = child.ControlTypeName or ''
                    cn = child.Name or ''
                    if ct == 'ButtonControl':
                        # Check InvokePattern
                        try:
                            ip = child.GetInvokePattern()
                            if ip:
                                if not cn:  # Empty name = relay button
                                    return child
                                # Named button (avatar) - keep searching deeper
                        except:
                            pass
                    result = find_relay_btn(child, depth + 1)
                    if result:
                        return result
            except:
                pass
            return None
        return find_relay_btn(root)

    def _find_relay_button_global(self) -> Optional[any]:
        """Search for relay button in the entire WeChat window."""
        return self._find_button_in_pane(self.window)

    @staticmethod
    def _win32_click(x: int, y: int):
        """Instant click via Win32 API (no cursor animation, no delay)."""
        ctypes.windll.user32.SetCursorPos(x, y)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP

    def _click_relay_button(self, btn) -> bool:
        """Click the relay button via Win32 mouse_event (instant)."""
        # t0 = time.time()
        self._ensure_window()
        try:
            if not self.window.IsActive():
                self.window.SetActive()
        except:
            pass
        # t_activate = time.time() - t0
        try:
            r = btn.BoundingRectangle
            if hasattr(r, 'left'):
                cx = (r.left + r.right) // 2
                cy = (r.top + r.bottom) // 2
            else:
                cx = (r[0] + r[2]) // 2
                cy = (r[1] + r[3]) // 2
            if cy <= 0:
                logging.warning(f"  按钮不在可视区域内 (y={cy})")
                return False
            self._win32_click(cx, cy)
            # logging.info(f"  点击方式: Win32.click({cx},{cy}) (激活{t_activate:.3f}s, 总{time.time()-t0:.3f}s)")
            return True
        except Exception as e:
            logging.warning(f"  Win32 点击失败: {e}")
        # Fallback: pyautogui
        try:
            r = btn.BoundingRectangle
            if hasattr(r, 'left'):
                cx = (r.left + r.right) // 2
                cy = (r.top + r.bottom) // 2
            else:
                cx = (r[0] + r[2]) // 2
                cy = (r[1] + r[3]) // 2
            pyautogui.click(cx, cy)
            logging.info(f"  点击方式: pyautogui(备份)")
            return True
        except:
            pass
        return False

    def _scan_relays(self) -> List[Tuple[any, str]]:
        """Fast scan: detect new relay messages. Returns [(item, text)]."""
        # t0 = time.time()
        msg_list = self._get_msg_list()
        if not msg_list:
            return []

        items = msg_list.GetChildren()
        relay_count = 0
        new_relay = None
        for item in items:
            try:
                if item.ControlTypeName != "ListItemControl":
                    continue
                if not self._is_relay_item(item):
                    continue
                relay_count += 1
                item_name = item.Name
                if self._is_new_relay(item_name):
                    new_relay = (item, item_name)
                    break
            except:
                continue

        # t_scan = time.time() - t0
        if relay_count > 0:
            logging.info(f"[扫描] 共 {len(items)} 项 {relay_count} 接龙"
                         f"{' — 发现新接龙!' if new_relay else ' — 无新接龙'}")

        return [new_relay] if new_relay else []

    def _find_chat_input(self) -> Optional[any]:
        """Find the main chat input box."""
        # Try 3-column layout first (fast path)
        try:
            cols = self._get_cols()
            if len(cols) >= 3:
                def search_col(ctrl, depth=0):
                    if depth > 8:
                        return None
                    try:
                        for child in ctrl.GetChildren():
                            t = child.ControlTypeName or ''
                            if t in ('EditControl', 'RichEditBox', 'DocumentControl'):
                                return child
                            r = search_col(child, depth + 1)
                            if r:
                                return r
                    except:
                        pass
                    return None
                result = search_col(cols[2])
                if result:
                    return result
        except:
            pass
        # Fallback: global recursive search for EditControl (skipping the search box named '搜索')
        def search_global(ctrl, depth=0):
            if depth > 12:
                return None
            try:
                for child in ctrl.GetChildren():
                    t = child.ControlTypeName or ''
                    n = child.Name or ''
                    if t == 'EditControl' and n != '搜索':
                        return child
                    r = search_global(child, depth + 1)
                    if r:
                        return r
            except:
                pass
            return None
        return search_global(self.window)

    # ---------- 参与接龙 ----------

    def _join_relay(self, relay_item: any, relay_text: str) -> bool:
        """Find the button, click it, paste response text, and send."""
        if not self._is_new_relay(relay_text):
            return False

        logging.info(f"正在参与接龙: '{relay_text[:40]}'")
        t_start = time.time()

        # # Benchmark logging overhead
        # _t = time.time()
        # logging.info(f"  日志开销测试")
        # log_cost = time.time() - _t

        self._joining = True
        self._joining_timeout = time.time() + 15

        try:
            # 1. Find the relay button (item first, then full window)
            t0 = time.time()
            btn = self._find_button_in_pane(relay_item)
            if not btn:
                btn = self._find_relay_button_global()
            if not btn:
                logging.warning("找不到参与接龙按钮")
                try:
                    for child in relay_item.GetChildren():
                        ct = child.ControlTypeName or '?'
                        cn = (child.Name or '')[:40]
                        logging.warning(f"  item子控件: [{ct}] '{cn}'")
                except:
                    pass
                return False

            # 2. Read button properties
            # t_prop = time.time()
            btn_name = btn.Name or '(空)'
            btn_type = btn.ControlTypeName or '?'
            try:
                ip = btn.GetInvokePattern()
                has_ip = '有Invoke' if ip else '无Invoke'
            except:
                has_ip = 'Invoke错误'
            try:
                r = btn.BoundingRectangle
                pos = f'({r.left},{r.top})-({r.right},{r.bottom})'
            except:
                pos = '未知位置'
            # t_prop_cost = time.time() - t_prop
            # t_find = time.time() - t0
            logging.info(f"  找到按钮: [{btn_type}] '{btn_name}' {has_ip} {pos}")

            # Click
            t_click = time.time()
            if not self._click_relay_button(btn):
                logging.warning("接龙按钮点击失败，跳过")
                return False
            t_click_cost = time.time() - t_click

            # 3. Wait for relay template
            time.sleep(0.01)

            # 4. Paste and send
            # t_paste = time.time()
            self._set_clipboard(self.config["response_text"])
            pyautogui.hotkey('ctrl', 'v')
            pyautogui.press('enter')
            # t_send = time.time() - t_paste

            self._mark_relay_done(relay_text)
            # total = time.time() - t_start
            logging.info(f"成功参与接龙！")
            return True

        except Exception as e:
            logging.error(f"参与接龙失败: {e}")
            return False
        finally:
            self._joining = False

    # ---------- 预加载 ----------

    def _preload_existing(self):
        """Mark all existing relay messages as already processed.
        Uses Name-based check only (fast) - no COM navigation per item."""
        msg_list = self._get_msg_list()
        if not msg_list:
            return

        count = 0
        for item in msg_list.GetChildren():
            try:
                if item.ControlTypeName != "ListItemControl":
                    continue
                item_name = item.Name
                # Relay messages start with #接龙
                if not item_name or not item_name.startswith("#"):
                    continue
                if self._is_new_relay(item_name):
                    self._mark_relay_done(item_name)
                    count += 1
            except:
                continue

        if count > 0:
            logging.info(f"已标记 {count} 个已有接龙")

    # ---------- 主循环 ----------

    def run(self):
        cfg = self.config

        logging.info("=" * 48)
        logging.info("  微信自动接龙机器人 v3.0")
        logging.info(f"  检测间隔: {cfg['check_interval']}秒")
        logging.info(f"  接龙内容: {cfg['response_text']}")
        if cfg["target_groups"]:
            logging.info(f"  监控群聊: {', '.join(cfg['target_groups'])}")
        else:
            logging.info("  监控模式: 当前打开的聊天")
        logging.info("=" * 48)

        if not self.connect():
            input("\n按 Enter 退出...")
            return

        # First group switch to enter target chat
        if cfg["target_groups"]:
            self._switch_to_next_group()
            self._last_group_switch = time.time()

        # Pre-mark existing relays
        logging.debug("准备预加载...")
        time.sleep(1)
        self._preload_existing()
        logging.debug("预加载完成")

        logging.info("开始监控（按 Ctrl+C 停止）\n")

        try:
            multi_group = len(cfg["target_groups"]) > 1
            tick = 0
            while True:
                # Check window still alive
                if not self.window.Exists():
                    logging.warning("微信窗口消失，尝试重新连接...")
                    if not self.connect():
                        time.sleep(5)
                        continue

                # Group switching (only for multi-group mode)
                if multi_group:
                    now = time.time()
                    if now - self._last_group_switch >= cfg["switch_group_interval"]:
                        self._switch_to_next_group()
                        self._last_group_switch = now
                        time.sleep(0.5)

                # Scroll to bottom to see latest messages
                self._scroll_msg_list()

                # Scan for new relays
                if not self._joining:
                    relays = self._scan_relays()
                    for item, text in relays:
                        self._join_relay(item, text)
                else:
                    # Check joining timeout (safety net)
                    if time.time() > self._joining_timeout:
                        self._joining = False

                tick += 1
                if tick % 20 == 0:
                    logging.info(f"监控中... (已扫描 {tick} 次)")
                time.sleep(cfg["check_interval"])

        except KeyboardInterrupt:
            logging.info("用户中断")
        except Exception as e:
            logging.critical(f"错误: {traceback.format_exc()}")
            input("\n按 Enter 退出...")


# ===== 入口 =====
if __name__ == "__main__":
    cfg = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}
    setup_logging(cfg.get("debug_mode", False))
    bot = RelayBot()
    bot.run()
