"""
微信自动接龙脚本 v2.0
========================
功能：监控微信群聊中的接龙消息，自动点击"参与接龙"并填写内容
原理：pyautogui + OpenCV 模板匹配（适配微信 4.0+）

使用方法：
  1. pip install pyautogui opencv-python-headless uiautomation pillow
  2. 修改 config.json
  3. 保持微信已登录，窗口打开，目标群聊可见
  4. 运行：python wechat_relay_bot.py
"""

import pyautogui
import cv2
import numpy as np
from PIL import Image
import uiautomation as auto
import json
import time
import logging
import os
import hashlib
import sys
import traceback
from typing import Optional, Tuple, List, Set

# ===== 路径 =====
BASE_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
PROCESSED_FILE = os.path.join(BASE_DIR, "processed_relays.json")
TEMPLATE_FILE = os.path.join(BASE_DIR, "relay_template.png")
LOG_FILE = os.path.join(BASE_DIR, "relay_bot.log")

# ===== 默认配置 =====
DEFAULT_CONFIG = {
    "check_interval": 2.0,
    "response_text": "跟一个",
    "target_groups": [],
    "switch_group_interval": 10,
    "template_threshold": 0.5,
    "debug_mode": False,
}

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1


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
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8', mode='a'),
            logging.StreamHandler()
        ]
    )


class RelayBot:
    def __init__(self):
        self.config = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}
        self.processed: Set[str] = set(load_json(PROCESSED_FILE, []))
        self.wechat_rect: Optional[Tuple[int, int, int, int]] = None
        self.window: Optional[auto.WindowControl] = None
        self.template = self._load_template()
        self._prev_chat_hash = None
        self._last_group_switch = 0
        self._current_group_index = -1
        self._chat_offset_x = 375  # Chat area X offset within window screenshot
        self._chat_width = 0
        self._chat_height = 0

    # ---------- 持久化 ----------

    def _save_processed(self):
        save_json(PROCESSED_FILE, list(self.processed))

    # ---------- 模板 ----------

    def _load_template(self):
        """加载"参与接龙"按钮模板"""
        t = Image.open(TEMPLATE_FILE) if os.path.exists(TEMPLATE_FILE) else None
        if t:
            t = t.convert('L')
            logging.info(f"模板已加载: {t.size}")
            return np.array(t)
        logging.warning("模板文件不存在，请先运行 --calibrate")
        return None

    # ---------- 窗口 ----------

    def connect(self) -> bool:
        logging.info("正在连接微信...")
        for i in range(30):
            try:
                win = auto.WindowControl(searchDepth=1, Name="微信")
                if win.Exists():
                    self.window = win
                    r = win.BoundingRectangle
                    self.wechat_rect = (r.left, r.top, r.right, r.bottom)
                    w_width = r.width()
                    w_height = r.height()
                    # Chat message area: right portion of the window
                    # Left toolbar ~68px, chat list ~308px, message area = rest
                    self._chat_offset_x = 375  # Approximate
                    self._chat_width = w_width - self._chat_offset_x
                    self._chat_height = w_height
                    logging.info(f"✓ 微信已连接 ({w_width}x{w_height})")
                    logging.info(f"  消息区域: ({r.left + self._chat_offset_x}, {r.top}) - "
                                 f"({r.right}, {r.bottom})")
                    return True
            except:
                pass
            if i == 0:
                logging.info("等待微信窗口...")
            time.sleep(1)
        logging.error("✗ 未找到微信窗口")
        return False

    # ---------- 截图 ----------

    def _screenshot_chat_area(self) -> Optional[np.ndarray]:
        """截取聊天消息区域（灰度）"""
        if not self.wechat_rect:
            return None
        left, top, right, bottom = self.wechat_rect
        try:
            s = pyautogui.screenshot(region=(
                left + self._chat_offset_x, top,
                self._chat_width, self._chat_height
            ))
            return cv2.cvtColor(np.array(s), cv2.COLOR_RGB2GRAY)
        except Exception as e:
            logging.debug(f"截图失败: {e}")
            return None

    def _chat_bottom_region(self, img: np.ndarray) -> np.ndarray:
        """取聊天区域底部 40%（接龙按钮出现的位置）"""
        h = img.shape[0]
        return img[int(h * 0.6):, :]

    # ---------- 变化检测 ----------

    def _has_new_content(self, img: np.ndarray) -> bool:
        """通过像素哈希快速判断聊天区域是否有新内容"""
        h = img.shape[0]
        bottom_part = img[int(h * 0.5):, :]
        # Downsample and hash
        small = cv2.resize(bottom_part, (32, 32))
        hval = hashlib.md5(small.tobytes()).hexdigest()[:16]

        if self._prev_chat_hash is None:
            self._prev_chat_hash = hval
            return False

        changed = hval != self._prev_chat_hash
        self._prev_chat_hash = hval
        return changed

    # ---------- 接龙检测 ----------

    def find_relay_button(self) -> Optional[Tuple[int, int]]:
        """用模板匹配找"参与接龙"按钮，返回屏幕绝对坐标"""
        if self.template is None:
            return None

        chat_img = self._screenshot_chat_area()
        if chat_img is None:
            return None

        th, tw = self.template.shape

        # 先快速检查底部区域是否有变化
        if self._prev_chat_hash is not None and not self._has_new_content(chat_img):
            return None

        # 多尺度模板匹配
        best_score = 0
        best_pos = None

        for scale in [1.0, 0.85, 0.7]:
            if scale != 1.0:
                scaled_tpl = cv2.resize(self.template, None, fx=scale, fy=scale,
                                        interpolation=cv2.INTER_AREA)
            else:
                scaled_tpl = self.template

            sth, stw = scaled_tpl.shape
            if sth > chat_img.shape[0] or stw > chat_img.shape[1]:
                continue

            try:
                res = cv2.matchTemplate(chat_img, scaled_tpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
            except:
                continue

            if max_val > best_score:
                best_score = max_val
                # Convert to absolute screen coordinates
                btn_center_x = (self.wechat_rect[0] + self._chat_offset_x
                                + max_loc[0] + stw // 2)
                btn_center_y = self.wechat_rect[1] + max_loc[1] + sth // 2
                best_pos = (btn_center_x, btn_center_y, max_val)

        if best_score >= self.config["template_threshold"]:
            logging.info(f"📋 发现接龙按钮 (confidence={best_score:.2f})")
            return best_pos

        return None

    # ---------- 参与接龙 ----------

    def join_relay(self, position: Tuple[int, int, int]) -> bool:
        """点击按钮并填写接龙内容"""
        x, y, confidence = position

        # 生成唯一指纹（用位置）
        fp = hashlib.md5(f"{x // 20},{y // 20}".encode()).hexdigest()[:16]
        if fp in self.processed:
            return False

        logging.info(f"🔄 正在参与接龙 ({x}, {y})")

        try:
            # 1. 点击按钮
            pyautogui.click(x, y)
            time.sleep(1.5)

            # 2. 等待对话框出现，输入内容
            pyautogui.typewrite(self.config["response_text"], interval=0.05)
            time.sleep(0.5)
            pyautogui.press('enter')
            time.sleep(0.5)

            self.processed.add(fp)
            self._save_processed()
            logging.info(f"✓ 成功参与接龙！")
            return True

        except Exception as e:
            logging.error(f"✗ 参与接龙失败: {e}")
            return False

    # ---------- 群聊切换 ----------

    def _navigate_to_group(self, name: str) -> bool:
        """用 Ctrl+F 搜索群聊并进入"""
        try:
            # 1. 确保微信窗口激活
            if self.window:
                try:
                    self.window.SetActive()
                except:
                    pass
            time.sleep(0.3)

            # 2. Ctrl+F 打开搜索
            pyautogui.hotkey('ctrl', 'f')
            time.sleep(0.5)

            # 3. 输入群名
            pyautogui.typewrite(name, interval=0.03)
            time.sleep(0.8)

            # 4. Enter 进入第一个结果
            pyautogui.press('enter')
            time.sleep(1.5)
            return True

        except Exception as e:
            logging.error(f"切换到群聊「{name}」失败: {e}")
            return False

    def _switch_to_next_group(self):
        groups = self.config.get("target_groups", [])
        if not groups:
            return

        self._current_group_index = (self._current_group_index + 1) % len(groups)
        name = groups[self._current_group_index]
        logging.info(f"切换到群聊: {name}")

        for attempt in range(2):
            if self._navigate_to_group(name):
                # 切换后重置变化检测，避免把旧内容当作新消息
                self._prev_chat_hash = None
                # 立即扫一次，把已有的接龙标记掉
                time.sleep(1)
                pos = self.find_relay_button()
                if pos:
                    self.join_relay(pos)
                return
            time.sleep(2)

        logging.error(f"群聊「{name}」切换失败，跳过")

    # ---------- 校准 ----------

    def calibrate(self):
        """校准模式：截取微信窗口，保存模板"""
        logging.info("=" * 48)
        logging.info("  校准模式")
        logging.info("=" * 48)

        if not self.connect():
            return

        print("\n请在微信中打开一个包含接龙消息的群聊")
        print("确保「参与接龙」按钮在聊天区域可见")
        input("准备好后按 Enter 键截图...")

        chat = self._screenshot_chat_area()
        if chat is None:
            print("截图失败")
            return

        # 保存截图供手动标注
        cv2.imwrite(os.path.join(BASE_DIR, "calibrate_chat.png"), chat)
        print(f"\n聊天区域截图已保存: calibrate_chat.png ({chat.shape[1]}x{chat.shape[0]})")
        print("请告诉我截图里「参与接龙」按钮的 X, Y 坐标")
        print("你可以用画图工具打开该文件查看坐标")

    # ---------- 预加载 ----------

    def _preload_existing(self):
        """启动时扫描已有接龙并标记"""
        pos = self.find_relay_button()
        if pos:
            x, y, conf = pos
            fp = hashlib.md5(f"{x // 20},{y // 20}".encode()).hexdigest()[:16]
            if fp not in self.processed:
                self.processed.add(fp)
                self._save_processed()
                logging.info(f"已标记 1 个运行前已存在的接龙")

    # ---------- 主循环 ----------

    def run(self):
        cfg = self.config

        logging.info("=" * 48)
        logging.info("  微信自动接龙机器人 v2.0")
        logging.info(f"  检测间隔: {cfg['check_interval']}秒")
        logging.info(f"  接龙内容: {cfg['response_text']}")
        logging.info(f"  匹配阈值: {cfg['template_threshold']}")
        if cfg["target_groups"]:
            logging.info(f"  监控群聊: {', '.join(cfg['target_groups'])}")
        else:
            logging.info("  监控模式: 当前打开的聊天")
        logging.info("=" * 48)

        if not self.connect():
            input("\n按 Enter 退出...")
            return

        if self.template is None:
            logging.error("模板未加载，请先运行 --calibrate")
            input("\n按 Enter 退出...")
            return

        # 预标记已有接龙
        self._preload_existing()

        logging.info("▶ 开始监控（按 Ctrl+C 停止）\n")

        try:
            while True:
                # 群聊切换
                if cfg["target_groups"]:
                    now = time.time()
                    if now - self._last_group_switch >= cfg["switch_group_interval"]:
                        self._switch_to_next_group()
                        self._last_group_switch = now

                # 检测并参与接龙
                pos = self.find_relay_button()
                if pos:
                    self.join_relay(pos)

                time.sleep(cfg["check_interval"])

        except KeyboardInterrupt:
            logging.info("⏹ 用户中断")
        except Exception as e:
            logging.critical(f"❌ 错误: {traceback.format_exc()}")
            input("\n按 Enter 退出...")


# ===== 入口 =====
if __name__ == "__main__":
    cfg = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}
    setup_logging(cfg.get("debug_mode", False))
    bot = RelayBot()

    if "--calibrate" in sys.argv:
        bot.calibrate()
    else:
        bot.run()
