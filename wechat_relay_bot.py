"""
微信自动接龙脚本 v1.1
========================
功能：监控微信群聊中的接龙消息，自动点击"参与接龙"并填写内容
特点：每条接龙仅自动参与一次，已处理的接龙会记录到文件，重启也不会重复
原理：通过 uiautomation 控制 Windows 微信客户端的 UI 元素

使用方法：
  1. 确保已安装依赖：pip install uiautomation
  2. 修改 config.json 中的配置（接龙内容、目标群聊等）
  3. 保持微信已登录且窗口打开（可最小化到托盘）
  4. 运行：python wechat_relay_bot.py
  5. 按 Ctrl+C 停止运行

注意：
  - 脚本运行期间请勿用鼠标点击接龙区域，以免干扰
  - 首次运行建议开启 debug_mode 观察日志
  - 如果指定了 target_groups，脚本会自动切换群聊
"""

import uiautomation as auto
import json
import time
import logging
import os
import hashlib
import sys
import traceback
from datetime import datetime
from typing import List, Optional, Set

# ===== 文件路径 =====
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
PROCESSED_FILE = os.path.join(os.path.dirname(__file__), "processed_relays.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "relay_bot.log")

# ===== 默认配置 =====
DEFAULT_CONFIG = {
    "check_interval": 3.0,          # 检测间隔（秒）
    "response_text": "跟一个",       # 接龙时发送的内容
    "target_groups": [],             # 目标群聊名称列表，留空则只监控当前打开的聊天
    "switch_group_interval": 10,     # 多个群聊时的切换间隔（秒）
    "debug_mode": False,             # 开启后记录更详细的日志
}


# ===== JSON 工具函数 =====
def load_json(path: str, default=None):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"读取 {path} 失败: {e}")
    return default if default is not None else {}


def save_json(path: str, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ===== 日志配置 =====
def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8', mode='a'),
            logging.StreamHandler()
        ]
    )


# ===== 主机器人 =====
class RelayBot:
    """微信自动接龙机器人"""

    def __init__(self):
        self.config = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}
        self.processed: Set[str] = set(load_json(PROCESSED_FILE, []))
        self.wechat_window: Optional[auto.WindowControl] = None
        self._last_group_switch = 0
        self._current_group_index = -1

    # ---------- 持久化 ----------

    def _save_processed(self):
        save_json(PROCESSED_FILE, list(self.processed))

    # ---------- 控件指纹（用于去重） ----------

    def _fingerprint(self, control) -> str:
        """为接龙按钮生成唯一指纹，同一接龙只参与一次"""
        try:
            r = control.BoundingRectangle
            parent_text = ""
            try:
                p = control.GetParentControl()
                if p and p.Name:
                    parent_text = p.Name[:80]
            except:
                pass
            raw = f"{r.left}_{r.top}_{r.right}_{r.bottom}_{parent_text}"
            return hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]
        except Exception:
            return f"fp_{time.time_ns()}"

    # ---------- UI 控件搜索 ----------

    def _find_controls_deep(self, root, predicate, max_depth=8, _depth=0) -> list:
        """深度优先遍历查找所有符合条件的控件"""
        if _depth > max_depth:
            return []
        results = []
        try:
            if predicate(root):
                results.append(root)
            for child in root.GetChildren():
                results.extend(
                    self._find_controls_deep(child, predicate, max_depth, _depth + 1)
                )
        except Exception:
            pass
        return results

    # ---------- 连接微信 ----------

    def connect(self) -> bool:
        """查找并连接到微信窗口"""
        logging.info("正在查找微信窗口...")

        for i in range(60):  # 最多等 60 秒
            try:
                win = auto.WindowControl(searchDepth=1, Name="微信")
                if win.Exists():
                    self.wechat_window = win
                    try:
                        win.Show()
                        win.SetActive()
                    except Exception:
                        pass
                    logging.info("✓ 已连接到微信")
                    # 等待聊天列表加载就绪
                    self._wait_chatlist_ready()
                    return True
            except Exception:
                pass

            if i == 0:
                logging.info("等待微信窗口出现 ...")
                logging.info("请确保：① 微信已登录  ② 微信窗口未关闭（可最小化到托盘）")
            time.sleep(1)

        logging.error("✗ 未找到微信窗口！请先启动微信并登录。")
        return False

    def _wait_chatlist_ready(self, timeout: float = 15.0):
        """等待聊天列表加载完成（重登后 UI 重建需要时间）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                items = self._find_controls_deep(
                    self.wechat_window,
                    lambda c: (
                        isinstance(c, auto.ListItemControl)
                        and c.Exists()
                        and c.Name
                        and len(c.Name.strip()) > 0
                    ),
                    max_depth=6,
                )
                if len(items) >= 1:
                    logging.debug(f"聊天列表已就绪（{len(items)} 个条目）")
                    return
            except Exception:
                pass
            time.sleep(0.5)
        logging.warning("聊天列表加载超时（可能窗口未完全就绪）")

    def _is_window_alive(self) -> bool:
        """检查微信窗口是否还活着"""
        try:
            return self.wechat_window is not None and self.wechat_window.Exists()
        except Exception:
            return False

    # ---------- 查找接龙按钮 ----------

    def find_relays(self) -> list:
        """在当前聊天界面中查找所有'参与接龙'按钮"""
        if not self.wechat_window:
            return []

        try:
            buttons = self._find_controls_deep(
                self.wechat_window,
                lambda c: (
                    isinstance(c, auto.ButtonControl)
                    and c.Exists()
                    and c.Name
                    and "参与接龙" in c.Name
                ),
                max_depth=8,
            )

            if buttons:
                logging.info(f"📋 当前发现 {len(buttons)} 个接龙")
            return buttons

        except Exception as e:
            logging.debug(f"查找接龙按钮时出错: {e}")
            return []

    # ---------- 参与接龙 ----------

    def join_relay(self, button) -> bool:
        """点击接龙按钮、填写内容、发送"""
        fp = self._fingerprint(button)
        if fp in self.processed:
            return False  # 已处理过

        logging.info("🔄 发现新接龙，正在参与...")

        try:
            # 1. 点击按钮
            button.Click()
            time.sleep(1.5)

            # 2. 查找弹出的接龙对话框
            dialog = self._find_relay_dialog()
            if not dialog:
                logging.info("未检测到接龙对话框，标记为已处理")
                self.processed.add(fp)
                self._save_processed()
                return True

            # 3. 激活对话框
            self._activate_window(dialog)
            time.sleep(0.5)

            # 4. 找到输入框并输入内容
            edit = self._find_edit_in_dialog(dialog)
            if not edit:
                logging.error("未找到接龙输入框，跳过")
                return False

            edit.Click()
            time.sleep(0.3)

            # 清除现有内容再输入
            try:
                edit.SendKeys('{Ctrl}a')
                time.sleep(0.1)
                edit.SendKeys('{Delete}')
                time.sleep(0.1)
            except:
                pass

            edit.SendKeys(self.config["response_text"])
            time.sleep(0.5)

            # 5. 点击发送按钮
            sent = self._click_send(dialog, edit)

            time.sleep(0.5)
            self.processed.add(fp)
            self._save_processed()
            logging.info(f"✓ 成功参与接龙！")
            return True

        except Exception as e:
            logging.error(f"✗ 参与接龙失败: {traceback.format_exc()}")
            return False

    def _find_relay_dialog(self):
        """查找接龙弹窗（多种可能的窗口名）"""
        for name in ["接龙", "接龙详情", "参与接龙"]:
            try:
                dlg = auto.WindowControl(Name=name)
                if dlg.Exists():
                    return dlg
            except:
                continue
        # 在微信窗口内部找
        if self.wechat_window:
            try:
                children = self.wechat_window.GetChildren()
                for c in children:
                    if "接龙" in (c.Name or ""):
                        return c
            except:
                pass
        return None

    def _activate_window(self, win):
        """尝试激活窗口"""
        try:
            win.Show()
            win.SetActive()
        except:
            pass

    def _find_edit_in_dialog(self, dialog):
        """在对话框中查找可输入的编辑框"""
        try:
            edit = dialog.EditControl(searchDepth=5)
            if edit.Exists():
                return edit
        except:
            pass
        try:
            edit = dialog.PaneControl(searchDepth=5).EditControl()
            if edit.Exists():
                return edit
        except:
            pass
        # 深层搜索
        edits = self._find_controls_deep(
            dialog,
            lambda c: isinstance(c, auto.EditControl) and c.Exists(),
            max_depth=6,
        )
        if edits:
            return edits[0]
        return None

    def _click_send(self, dialog, edit) -> bool:
        """点击发送/确认按钮"""
        for send_name in ["发送", "完成", "确定", "提交"]:
            try:
                btn = dialog.ButtonControl(Name=send_name)
                if btn.Exists():
                    btn.Click()
                    return True
            except:
                continue
        # 兜底：按 Enter
        try:
            edit.SendKeys("{Enter}")
            return True
        except:
            return False

    # ---------- 群聊切换 ----------

    def _switch_to_next_group(self):
        """切换到下一个目标群聊，带重试"""
        groups = self.config.get("target_groups", [])
        if not groups or not self.wechat_window or not self._is_window_alive():
            return

        self._current_group_index = (self._current_group_index + 1) % len(groups)
        group_name = groups[self._current_group_index]
        logging.info(f"切换到群聊: {group_name}")

        for attempt in range(2):
            try:
                # 在整个微信窗口中深度搜索匹配群聊名称的可点击控件
                items = self._find_controls_deep(
                    self.wechat_window,
                    lambda c: (
                        c.Exists()
                        and c.Name
                        and group_name == c.Name.strip()
                        and isinstance(c, (auto.ListItemControl, auto.PaneControl, auto.ButtonControl))
                    ),
                    max_depth=8,
                )

                if items:
                    items[0].Click()
                    time.sleep(2)
                    return

                # 第一轮失败：打印当前聊天列表条目辅助排查
                if attempt == 0:
                    all_items = self._find_controls_deep(
                        self.wechat_window,
                        lambda c: (
                            isinstance(c, auto.ListItemControl)
                            and c.Exists()
                            and c.Name
                        ),
                        max_depth=6,
                    )
                    names = [c.Name for c in all_items[:20]]
                    logging.warning(
                        f"未找到群聊「{group_name}」，当前可见条目 ({len(all_items)}): "
                        f"{'、'.join(names) if names else '空'}"
                    )
                    logging.info(f"3 秒后重试...")
                    time.sleep(3)
                else:
                    logging.error(f"群聊「{group_name}」始终未找到，跳过")
            except Exception as e:
                logging.error(f"切换群聊「{group_name}」失败: {e}")
                time.sleep(1)

    # ---------- 主循环 ----------

    def run(self):
        """主运行循环"""
        cfg = self.config

        logging.info("=" * 48)
        logging.info("  微信自动接龙机器人")
        logging.info(f"  检查间隔: {cfg['check_interval']}秒")
        logging.info(f"  接龙内容: {cfg['response_text']}")
        if cfg["target_groups"]:
            logging.info(f"  监控群聊: {', '.join(cfg['target_groups'])}")
        else:
            logging.info("  监控模式: 当前打开的聊天")
        logging.info(f"  已处理接龙: {len(self.processed)} 条")
        logging.info("=" * 48)

        if not self.connect():
            input("\n按 Enter 退出...")
            return

        logging.info("▶ 开始监控（按 Ctrl+C 停止）\n")

        try:
            while True:
                # 保活检查：窗口退出/崩溃时尝试重连
                if not self._is_window_alive():
                    logging.warning("微信窗口已断开，尝试重连...")
                    self.wechat_window = None
                    if not self.connect():
                        logging.error("重连失败，等待 10 秒后重试")
                        time.sleep(10)
                        continue

                # 群聊切换
                if self.config["target_groups"]:
                    now = time.time()
                    if now - self._last_group_switch >= self.config["switch_group_interval"]:
                        self._switch_to_next_group()
                        self._last_group_switch = now

                # 扫描接龙
                relays = self.find_relays()
                for btn in relays:
                    self.join_relay(btn)

                time.sleep(cfg["check_interval"])

        except KeyboardInterrupt:
            logging.info("\n⏹ 用户中断运行")
        except Exception as e:
            logging.critical(f"❌ 运行时错误: {traceback.format_exc()}")
            input("\n按 Enter 退出...")


# ===== 程序入口 =====
if __name__ == "__main__":
    # 加载配置（先于日志配置）
    cfg = {**DEFAULT_CONFIG}
    try:
        cfg = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}
    except:
        pass

    setup_logging(cfg.get("debug_mode", False))
    bot = RelayBot()
    bot.run()
