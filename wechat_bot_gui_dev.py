import os
import sys
import json
import time
import subprocess
import tempfile
import threading
import psutil
import requests
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# Force UTF-8 stdout
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

if getattr(sys, 'frozen', False):
    WORKSPACE_DIR = os.path.dirname(sys.executable)
else:
    WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(WORKSPACE_DIR, "wechat_config_dev.json")
LOG_FILE = os.path.join(WORKSPACE_DIR, "wechat_bot_dev.log")
PID_FILE = os.path.join(WORKSPACE_DIR, "wechat_bot_dev.pid")
BOT_SCRIPT = os.path.join(WORKSPACE_DIR, "wechat_ai_bot_dev.py")
KILL_SCRIPT = os.path.join(WORKSPACE_DIR, "kill_wechat_bot_dev.py")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)

class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT)
    ]

def get_wechat_normal_rect(hwnd):
    """获取微信在正常桌面状态下的坐标与长宽 (即便当前最小化或离屏也能准确提取)"""
    try:
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            r = wp.rcNormalPosition
            w = r.right - r.left
            h = r.bottom - r.top
            if w >= 400 and h >= 300 and r.left >= -500 and r.top >= -500:
                return [int(r.left), int(r.top), int(w), int(h)]
    except Exception:
        pass
    try:
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w >= 400 and h >= 300 and rect.left >= -500 and rect.top >= -500:
            return [int(rect.left), int(rect.top), int(w), int(h)]
    except Exception:
        pass
    return None

def ensure_default_desktop():
    try:
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
            user32.CloseDesktop(h_desk)
    except Exception:
        pass

def get_wechat_hwnd():
    ensure_default_desktop()
    candidates = []
    def enum_proc(hwnd, lp):
        class_buff = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buff, 256)
        c = class_buff.value
        if "Qt51514QWindowIcon" in c or "WeChatMainWndForPC" in c:
            wname = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, wname, 256)
            t = wname.value
            # 微信 4.0 伴随空壳代理窗口标题固定为纯英文 "Weixin"
            is_shell = (t == "Weixin")
            ver = 4 if "Qt" in c else 3
            candidates.append((hwnd, ver, t, is_shell))
            if is_shell:
                try:
                    user32.ShowWindow(hwnd, 0) # SW_HIDE 静默隐藏空壳代理窗口
                except Exception:
                    pass
        return True
        
    cb = WNDENUMPROC(enum_proc)
    user32.EnumWindows(cb, 0)
    
    # 严格优先选取真实主聊天窗口 (排除空壳窗口)
    real_candidates = [c for c in candidates if not c[3]]
    if real_candidates:
        return real_candidates[0][0], real_candidates[0][1]
    if candidates:
        return candidates[0][0], candidates[0][1]
    return None, None

DEFAULT_PROMPTS = {
    "🌟 真人日常 (默认)": "你现在是微信账号的主人（真实本人）。请以我（主人本人）的第一人称、自然口语、亲切随和的日常语气直接回复微信好友的消息。就像我本人在用手机打字聊天一样自然。严禁透露任何AI、机器人或助理身份，严禁使用客套机械的客服腔，严禁输出任何解释或markdown格式，直接输出我回复的那句话。",
    "🤣 随和幽默 / 接梗达人": "你现在是微信账号的主人（真实本人）。请以极具幽默感、机智开朗、会接梗的真人语气回复好友。善于敏锐捕捉对方话里的笑点与细节，秒懂热梗并顺势幽默抛梗接招，说话风趣机敏、轻松洒脱、情商极高，让人觉得和你聊天特别有意思、超解压。严禁严肃机械，严禁使用客服腔，严禁透露AI身份，直接输出幽默自然的回复短句。",
    "🌹 暧昧推拉 / 情感高手": "你现在是真实号主本人，正在回复暧昧对象。语气要有高情商的推拉感和适度的情感张力，既幽默风趣又带一点恰到好处的撩人和嘴甜。懂分寸、不油腻、欲擒故纵，善于发现对方话里的细节并给出有趣的互动。严禁生硬直白，严禁透露AI身份，直接输出真实聊天的短句。",
    "💕 甜蜜恋人 / 女友专属": "你现在是我的替身，正在回复我的女朋友/老婆。请以真实男友的第一人称回复，语气超级宠溺、温柔、体贴且求生欲拉满。多用爱称（如宝贝、宝宝、乖等），多给足情绪价值，关心她的状态并主动报备，说话带点撒娇和甜意，严禁任何直男式说教或敷衍，严禁透露AI身份，直接输出日常打字的一两句话。",
    "😂 毒舌互怼 / 死党互损": "你现在是真实号主本人，正在回复多年的铁哥们/死党。语气要毒舌、嘴硬、爱开玩笑互损，毫不留情面地疯狂吐槽和接招，对方发什么都要幽默地怼回去或调侃两句，满嘴跑火车但感情很铁。严禁客套礼貌，严禁使用敬语，严禁透露AI身份，直接输出回怼的微信短句。",
    "⚔️ 阴阳怪气 / 仇敌对线": "你现在是真实号主本人，正在回复讨厌的人或仇敌。语气要高冷、带有极具杀伤力的高级阴阳怪气与不屑。回怼精准有力、直击要害、体面扎心且绝不吃亏，不带脏字却能把对方气得半死。严禁客套，严禁热情，严禁透露AI身份，直接输出冷嘲热讽的回复。",
    "💼 职场专业 / 严谨得体": "你现在是微信账号的主人（真实本人）。请以成熟稳重、严谨得体、高效专业的职场沟通语气回复消息。态度谦逊有礼，表达逻辑清晰、条理分明，用词规范严谨，展现出极高的职业素养。严禁轻浮玩笑，严禁透露AI身份，直接输出专业得体的回复。",
    "⏳ 忙碌代接 / 稍后细聊": "你现在是微信账号的主人（真实本人）。请以短平快的日常语气回复好友，告知自己当前正处于忙碌状态（如正在开会、开车或手头有急事），先简要确认已收到消息，并主动说明稍后空下来立刻详细回复。态度亲切真诚，严禁透露AI身份，直接输出1句话。",
    "🧊 高冷简洁 / 话少字精": "你现在是微信账号的主人（真实本人）。请以高冷、极简、话少字精的语气回复好友。回答直奔主题，通常只用几个字或一小句话（如“好”、“收到”、“晚点说”、“行”），绝不多说半句废话，言简意赅。严禁热情啰嗦，严禁透露AI身份，直接输出极短的微信回复。",
    "☕ 温和体贴 / 治愈倾听": "你现在是微信账号的主人（真实本人）。请以温暖、沉稳、善解人意且富有同理心的语气回复好友。认真倾听对方的心情，给予充分的理解、肯定与情绪支撑，让人感到被重视和治愈。严禁冷淡评判或讲大道理，严禁透露AI身份，直接输出温暖真诚的话。",
    "✨ 元气可爱 / 活泼软萌": "你现在是微信账号的主人（真实本人）。请以元气满满、甜美活泼、软萌可爱的语气回复好友。说话积极开朗，适当带上可爱的语气助词（如呀、呢、哇、啦、~、哈哈等），像一个阳光充满活力的小太阳。严禁死板严肃，严禁透露AI身份，直接输出充满元气的可爱短句。",
    "✏️ 自由定制": "你现在是微信账号的主人（真实本人）。请以我本人的第一人称、自然随和的语气直接回复好友，直接输出回复内容。"
}

class WeChatBotGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("微信AI自动回复")
        self.geometry("980x720")
        self.minsize(920, 640)
        
        ico_file = os.path.join(WORKSPACE_DIR, "wechat_sky_blue_unread_ai_v4.ico")
        if os.path.exists(ico_file):
            try:
                self.iconbitmap(ico_file)
            except Exception:
                pass
        
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("vista")
        except Exception:
            pass

        self.config_data = self.load_config()
        self.log_pos = 0
        self.is_monitoring = True
        
        self.create_widgets()
        self.load_settings_to_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_background_monitors()

    def on_close(self):
        try:
            hwnd, _ = get_wechat_hwnd()
            if hwnd:
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if rect.left < -500 or rect.top < -500:
                    saved = self.config_data.get("saved_window_rect", [100, 100, 1100, 750])
                    user32.ShowWindow(hwnd, 9) # SW_RESTORE
                    user32.SetWindowPos(hwnd, 0, saved[0], saved[1], saved[2], saved[3], 0x0040 | 0x0010)
        except Exception:
            pass
        self.is_monitoring = False
        self.destroy()

    def load_config(self):
        default = {
            "ai_engine": "openai_api",
            "openai_api": {
                "api_key": "",
                "api_base": "https://api.xiaomimimo.com/v1",
                "model": "mimo-v2.6-flash",
                "temperature": 0.7
            },
            "system_prompt": DEFAULT_PROMPTS["🌟 真人日常 (默认)"],
            "reply_prefix": "[AI自动回复] ",
            "include_prefix": False,
            "whitelist_mode": True,
            "whitelist": [],
            "blacklist": [
                "微信团队",
                "文件传输助手",
                "腾讯新闻",
                "微信支付",
                "服务通知",
                "订阅号消息"
            ],
            "reply_delay_seconds": 0.0,
            "check_interval_seconds": 0.8,
            "auto_focus_wechat": False,
            "ignored_keywords": [
                "按住鼠标",
                "语音输入文字",
                "按住说话",
                "按Enter发送",
                "发送(S)"
            ]
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    default.update(loaded)
            except Exception as e:
                print(f"Error loading config: {e}")
        return default

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config_data, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            messagebox.showerror("保存失败", f"保存配置文件失败: {e}")
            return False

    def create_widgets(self):
        # 1. Top Header Status Bar
        top_frame = ttk.LabelFrame(self, text=" 📊 系统状态与快捷控制 ", padding=(12, 8))
        top_frame.pack(fill=tk.X, padx=12, pady=6)

        # Status Indicators
        status_left = ttk.Frame(top_frame)
        status_left.pack(side=tk.LEFT, fill=tk.Y)
        
        self.lbl_wx_status = ttk.Label(status_left, text="⚪ 微信客户端: 检测中...", font=("微软雅黑", 9, "bold"))
        self.lbl_wx_status.pack(side=tk.LEFT, padx=(0, 20))

        self.lbl_bot_status = ttk.Label(status_left, text="⚪ AI 守护进程: 正在检测...", font=("微软雅黑", 9, "bold"))
        self.lbl_bot_status.pack(side=tk.LEFT, padx=10)

        # Action Buttons
        btn_box = ttk.Frame(top_frame)
        btn_box.pack(side=tk.RIGHT)

        self.btn_start = ttk.Button(btn_box, text="🚀 启动 AI 自动回复", command=self.start_bot)
        self.btn_start.pack(side=tk.LEFT, padx=4)

        self.btn_stop = ttk.Button(btn_box, text="⏹️ 停止守护", command=self.stop_bot)
        self.btn_stop.pack(side=tk.LEFT, padx=4)

        self.btn_hide_wx = ttk.Button(btn_box, text="👻 隐藏微信至离屏", command=self.toggle_offscreen_wechat)
        self.btn_hide_wx.pack(side=tk.LEFT, padx=4)

        self.btn_restart = ttk.Button(btn_box, text="🔄 重启服务", command=self.restart_bot)
        self.btn_restart.pack(side=tk.LEFT, padx=4)

        # 2. Main Notebook (Tabs)
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        self.tab_engine = ttk.Frame(self.notebook, padding=12)
        self.tab_prompt = ttk.Frame(self.notebook, padding=12)
        self.tab_whitelist = ttk.Frame(self.notebook, padding=12)
        self.tab_test = ttk.Frame(self.notebook, padding=12)

        self.notebook.add(self.tab_engine, text=" ⚙️ AI 引擎设置 ")
        self.notebook.add(self.tab_prompt, text=" 🎭 人设与指令 Prompt ")
        self.notebook.add(self.tab_whitelist, text=" 🌓 白名单与防扰 ")
        self.notebook.add(self.tab_test, text=" 🧪 AI 仿真测试沙盒 ")

        self.build_tab_engine()
        self.build_tab_prompt()
        self.build_tab_whitelist()
        self.build_tab_test()

        # Bottom Log Viewer
        log_frame = ttk.LabelFrame(self, text=" 📜 实时运行日志 (Auto-Scroll) ", padding=(10, 6))
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 8))

        log_ctrl_bar = ttk.Frame(log_frame)
        log_ctrl_bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(log_ctrl_bar, text="实时日志流 (记录未读红点、好友消息、AI回复详情):", font=("微软雅黑", 8)).pack(side=tk.LEFT)
        
        ttk.Button(log_ctrl_bar, text="清空日志", command=self.clear_logs).pack(side=tk.RIGHT, padx=2)
        ttk.Button(log_ctrl_bar, text="打开日志文件", command=self.open_log_file).pack(side=tk.RIGHT, padx=2)
        ttk.Button(log_ctrl_bar, text="📌 生成桌面快捷方式", command=self.create_desktop_shortcut).pack(side=tk.RIGHT, padx=4)

        self.txt_log = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=8, font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        self.txt_log.pack(fill=tk.BOTH, expand=True)

    # 1. TAB: Engine
    def build_tab_engine(self):
        engine_box = ttk.Frame(self.tab_engine)
        engine_box.pack(fill=tk.X, pady=4)

        ttk.Label(engine_box, text="AI 响应引擎:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT, padx=(0, 10))
        self.var_engine = tk.StringVar(value="openai_api")

        rb2 = ttk.Radiobutton(engine_box, text="🌐 大模型通用 API (DeepSeek / OpenAI / Kimi / Ollama 等兼容接口)", variable=self.var_engine, value="openai_api", command=self.on_engine_change)
        rb2.pack(side=tk.LEFT)

        # OpenAI Group
        self.grp_openai = ttk.LabelFrame(self.tab_engine, text=" 🌐 大模型 API 配置 ", padding=10)
        self.grp_openai.pack(fill=tk.X, pady=8)

        # Base URL
        row_base = ttk.Frame(self.grp_openai)
        row_base.pack(fill=tk.X, pady=4)
        ttk.Label(row_base, text="API Base URL:", width=15).pack(side=tk.LEFT)
        self.ent_api_base = ttk.Entry(row_base)
        self.ent_api_base.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Label(row_base, text="(例如: https://api.deepseek.com/v1 或 http://127.0.0.1:11434/v1)", foreground="#666666", font=("微软雅黑", 8)).pack(side=tk.RIGHT)

        # API Key
        row_key = ttk.Frame(self.grp_openai)
        row_key.pack(fill=tk.X, pady=4)
        ttk.Label(row_key, text="API Key:", width=15).pack(side=tk.LEFT)
        self.ent_api_key = ttk.Entry(row_key, show="*")
        self.ent_api_key.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.btn_show_key = ttk.Button(row_key, text="显示", width=6, command=self.toggle_api_key_visibility)
        self.btn_show_key.pack(side=tk.RIGHT)

        # Model
        row_model = ttk.Frame(self.grp_openai)
        row_model.pack(fill=tk.X, pady=4)
        ttk.Label(row_model, text="模型 ID (Model ID):", width=16).pack(side=tk.LEFT)
        self.ent_model = ttk.Entry(row_model)
        self.ent_model.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Label(row_model, text="(例如: deepseek-chat, gpt-4o-mini, qwen2.5:7b)", foreground="#666666", font=("微软雅黑", 8)).pack(side=tk.RIGHT)

        # Parameters Group
        grp_params = ttk.LabelFrame(self.tab_engine, text=" ⚡ 回复格式与行为参数 ", padding=10)
        grp_params.pack(fill=tk.X, pady=8)

        row_p1 = ttk.Frame(grp_params)
        row_p1.pack(fill=tk.X, pady=4)
        self.var_prefix = tk.BooleanVar(value=False)
        self.chk_prefix = ttk.Checkbutton(row_p1, text="添加前缀标明 AI 回复:", variable=self.var_prefix)
        self.chk_prefix.pack(side=tk.LEFT, padx=(0, 6))
        self.ent_prefix = ttk.Entry(row_p1, width=20)
        self.ent_prefix.pack(side=tk.LEFT, padx=(0, 20))

        row_p2 = ttk.Frame(grp_params)
        row_p2.pack(fill=tk.X, pady=4)
        ttk.Label(row_p2, text="拟人回复延迟:").pack(side=tk.LEFT, padx=(0, 6))
        self.spin_delay = ttk.Spinbox(row_p2, from_=0.0, to=10.0, increment=0.5, width=6)
        self.spin_delay.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(row_p2, text="秒 (模拟真实打字思考时间，防高频回复)").pack(side=tk.LEFT)

        # Save Button
        btn_save_frame = ttk.Frame(self.tab_engine)
        btn_save_frame.pack(fill=tk.X, pady=10)
        ttk.Button(btn_save_frame, text="💾 保存所有配置并生效", command=self.save_ui_to_config).pack(side=tk.RIGHT)

    def toggle_api_key_visibility(self):
        if self.ent_api_key.cget("show") == "*":
            self.ent_api_key.configure(show="")
            self.btn_show_key.configure(text="隐藏")
        else:
            self.ent_api_key.configure(show="*")
            self.btn_show_key.configure(text="显示")

    def on_engine_change(self):
        pass

    # 2. TAB: Prompt
    def build_tab_prompt(self):
        top_p = ttk.Frame(self.tab_prompt)
        top_p.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(top_p, text="🎭 预设人设风格:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        self.cbo_presets = ttk.Combobox(top_p, values=list(DEFAULT_PROMPTS.keys()), state="readonly", width=24)
        self.cbo_presets.current(0)
        self.cbo_presets.pack(side=tk.LEFT, padx=(0, 10))
        self.cbo_presets.bind("<<ComboboxSelected>>", self.on_preset_change)

        ttk.Label(top_p, text="💡 提示: 选择上方预设后，可直接在下方编辑框进行深度定制微调。", foreground="#666666", font=("微软雅黑", 8)).pack(side=tk.LEFT)

        self.txt_prompt = scrolledtext.ScrolledText(self.tab_prompt, wrap=tk.WORD, height=14, font=("微软雅黑", 9))
        self.txt_prompt.pack(fill=tk.BOTH, expand=True, pady=4)

        btn_p = ttk.Frame(self.tab_prompt)
        btn_p.pack(fill=tk.X, pady=8)
        ttk.Button(btn_p, text="💾 保存人设 Prompt", command=self.save_ui_to_config).pack(side=tk.RIGHT)

    # 3. TAB: Whitelist
    def build_tab_whitelist(self):
        top_w = ttk.Frame(self.tab_whitelist)
        top_w.pack(fill=tk.X, pady=(0, 6))

        self.var_wl_mode = tk.BooleanVar(value=True)
        self.chk_wl = ttk.Checkbutton(top_w, text="🔒 开启白名单模式（开启后【仅回复】白名单中的指定好友，未在名单者不回复）", variable=self.var_wl_mode)
        self.chk_wl.pack(side=tk.LEFT)

        mid_w = ttk.Frame(self.tab_whitelist)
        mid_w.pack(fill=tk.BOTH, expand=True, pady=4)

        # Left: Whitelist
        left_w = ttk.LabelFrame(mid_w, text=" 🟢 允许自动回复的好友白名单 ", padding=8)
        left_w.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))

        self.listbox_wl = tk.Listbox(left_w, selectmode=tk.SINGLE, font=("微软雅黑", 9))
        self.listbox_wl.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        wl_ctrl = ttk.Frame(left_w)
        wl_ctrl.pack(fill=tk.X)
        self.ent_add_wl = ttk.Entry(wl_ctrl)
        self.ent_add_wl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(wl_ctrl, text="添加好友", command=self.add_whitelist).pack(side=tk.LEFT, padx=2)
        ttk.Button(wl_ctrl, text="删除选中", command=self.del_whitelist).pack(side=tk.LEFT, padx=2)

        # Right: Blacklist
        right_w = ttk.LabelFrame(mid_w, text=" 🚫 严格屏蔽黑名单 (群聊/服务号/指定联系人) ", padding=8)
        right_w.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(6, 0))

        self.listbox_bl = tk.Listbox(right_w, selectmode=tk.SINGLE, font=("微软雅黑", 9))
        self.listbox_bl.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        bl_ctrl = ttk.Frame(right_w)
        bl_ctrl.pack(fill=tk.X)
        self.ent_add_bl = ttk.Entry(bl_ctrl)
        self.ent_add_bl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(bl_ctrl, text="添加屏蔽", command=self.add_blacklist).pack(side=tk.LEFT, padx=2)
        ttk.Button(bl_ctrl, text="删除选中", command=self.del_blacklist).pack(side=tk.LEFT, padx=2)

    # 4. TAB: Test Sandbox
    def build_tab_test(self):
        ttk.Label(self.tab_test, text="🧪 模拟微信好友提问，测试当前 AI 引擎与 Prompt 的真实回复质量:", font=("微软雅黑", 9, "bold")).pack(anchor=tk.W, pady=(0, 6))

        in_frame = ttk.Frame(self.tab_test)
        in_frame.pack(fill=tk.X, pady=4)

        ttk.Label(in_frame, text="好友昵称:").pack(side=tk.LEFT, padx=(0, 4))
        self.ent_test_sender = ttk.Entry(in_frame, width=14)
        self.ent_test_sender.insert(0, "测试好友")
        self.ent_test_sender.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(in_frame, text="发送内容:").pack(side=tk.LEFT, padx=(0, 4))
        self.ent_test_msg = ttk.Entry(in_frame)
        self.ent_test_msg.insert(0, "在干嘛呢？晚上一起吃个饭不？")
        self.ent_test_msg.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.btn_run_test = ttk.Button(in_frame, text="🚀 触发 AI 拟真回复测试", command=self.run_sandbox_test)
        self.btn_run_test.pack(side=tk.RIGHT)

        ttk.Label(self.tab_test, text="AI 回复输出 (与微信聊天完全一致):", font=("微软雅黑", 8)).pack(anchor=tk.W, pady=(8, 2))
        self.txt_test_out = scrolledtext.ScrolledText(self.tab_test, wrap=tk.WORD, height=10, font=("微软雅黑", 9))
        self.txt_test_out.pack(fill=tk.BOTH, expand=True, pady=4)

    # Load / Save Logic
    def load_settings_to_ui(self):
        c = self.config_data
        oa = c.get("openai_api", {})
        self.ent_api_base.delete(0, tk.END)
        self.ent_api_base.insert(0, oa.get("api_base", "https://api.xiaomimimo.com/v1"))
        self.ent_api_key.delete(0, tk.END)
        self.ent_api_key.insert(0, oa.get("api_key", ""))
        self.ent_model.delete(0, tk.END)
        self.ent_model.insert(0, oa.get("model", "mimo-v2.6-flash"))

        self.var_prefix.set(c.get("include_prefix", False))
        self.ent_prefix.delete(0, tk.END)
        self.ent_prefix.insert(0, c.get("reply_prefix", "[AI自动回复] "))
        self.spin_delay.set(c.get("reply_delay_seconds", 0.0))

        self.txt_prompt.delete("1.0", tk.END)
        self.txt_prompt.insert(tk.END, c.get("system_prompt", DEFAULT_PROMPTS["🌟 真人日常 (默认)"]))

        self.var_wl_mode.set(c.get("whitelist_mode", True))
        self.listbox_wl.delete(0, tk.END)
        for w in c.get("whitelist", []):
            self.listbox_wl.insert(tk.END, w)

        self.listbox_bl.delete(0, tk.END)
        for b in c.get("blacklist", []):
            self.listbox_bl.insert(tk.END, b)

    def save_ui_to_config(self, show_msg=True):
        self.config_data["ai_engine"] = self.var_engine.get()
        self.config_data["openai_api"] = {
            "api_key": self.ent_api_key.get().strip(),
            "api_base": self.ent_api_base.get().strip(),
            "model": self.ent_model.get().strip(),
            "temperature": 0.7
        }
        self.config_data["include_prefix"] = self.var_prefix.get()
        self.config_data["reply_prefix"] = self.ent_prefix.get()
        try:
            self.config_data["reply_delay_seconds"] = float(self.spin_delay.get())
        except Exception:
            self.config_data["reply_delay_seconds"] = 0.0

        self.config_data["system_prompt"] = self.txt_prompt.get("1.0", tk.END).strip()
        self.config_data["whitelist_mode"] = self.var_wl_mode.get()
        self.config_data["whitelist"] = list(self.listbox_wl.get(0, tk.END))
        self.config_data["blacklist"] = list(self.listbox_bl.get(0, tk.END))

        if self.save_config():
            if show_msg:
                messagebox.showinfo("保存成功", "✅ 配置已成功保存！后台 AI 守护进程将自动应用最新设置。")

    def on_preset_change(self, event=None):
        name = self.cbo_presets.get()
        if name in DEFAULT_PROMPTS:
            self.txt_prompt.delete("1.0", tk.END)
            self.txt_prompt.insert(tk.END, DEFAULT_PROMPTS[name])

    def add_whitelist(self):
        val = self.ent_add_wl.get().strip()
        if val:
            if val not in self.config_data["whitelist"]:
                self.config_data["whitelist"].append(val)
                self.listbox_wl.insert(tk.END, val)
                self.save_config()
            self.ent_add_wl.delete(0, tk.END)

    def del_whitelist(self):
        sel = self.listbox_wl.curselection()
        if sel:
            idx = sel[0]
            val = self.listbox_wl.get(idx)
            if val in self.config_data["whitelist"]:
                self.config_data["whitelist"].remove(val)
                self.save_config()
            self.listbox_wl.delete(idx)

    def add_blacklist(self):
        val = self.ent_add_bl.get().strip()
        if val:
            if val not in self.config_data["blacklist"]:
                self.config_data["blacklist"].append(val)
                self.listbox_bl.insert(tk.END, val)
                self.save_config()
            self.ent_add_bl.delete(0, tk.END)

    def del_blacklist(self):
        sel = self.listbox_bl.curselection()
        if sel:
            idx = sel[0]
            val = self.listbox_bl.get(idx)
            if val in self.config_data["blacklist"]:
                self.config_data["blacklist"].remove(val)
                self.save_config()
            self.listbox_bl.delete(idx)

    def run_sandbox_test(self):
        sender = self.ent_test_sender.get().strip() or "测试好友"
        msg = self.ent_test_msg.get().strip()
        if not msg:
            messagebox.showwarning("提示", "请输入发送内容！")
            return

        self.save_ui_to_config(show_msg=False)
        self.txt_test_out.delete("1.0", tk.END)
        self.txt_test_out.insert(tk.END, "🤖 正在调用大模型生成回复...\n")
        self.update()

        t0 = time.time()
        oa = self.config_data.get("openai_api", {})
        api_key = oa.get("api_key", "").strip()
        api_base = oa.get("api_base", "https://api.xiaomimimo.com/v1").rstrip("/")
        model = oa.get("model", "mimo-v2.6-flash")
        
        if not api_key:
            self.txt_test_out.delete("1.0", tk.END)
            self.txt_test_out.insert(tk.END, "❌ 请先在【AI 引擎设置】中填入有效的 API Key！")
            return
            
        url = f"{api_base}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        prompt = self.config_data.get("system_prompt", DEFAULT_PROMPTS["真人日常"])
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": msg}
            ],
            "temperature": 0.7
        }
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=12)
            if res.status_code == 200:
                data = res.json()
                reply = data["choices"][0]["message"]["content"].strip()
                dur = round(time.time() - t0, 2)
                self.txt_test_out.delete("1.0", tk.END)
                self.txt_test_out.insert(tk.END, f"{reply}\n\n[⏱️ 耗时: {dur} 秒]")
            else:
                self.txt_test_out.delete("1.0", tk.END)
                self.txt_test_out.insert(tk.END, f"❌ API 请求失败 (状态码 {res.status_code}):\n{res.text}")
        except Exception as e:
            self.txt_test_out.delete("1.0", tk.END)
            self.txt_test_out.insert(tk.END, f"❌ 请求大模型出错: {e}")

    def is_target_bot_process(self, proc):
        try:
            cmdline = proc.cmdline() or []
            cmd_str = " ".join(str(c) for c in cmdline).lower()
            name = (proc.name() or '').lower()
            # 严格排除 GUI、稳定版和其它
            if "gui" in name or "控制中心" in name or "wechat_bot_gui" in cmd_str:
                return False
            # 必须命中开发版特征 (wechat_ai_bot_dev 或 wechat_ai_bot_dev.exe 或 微信ai回复_开发版)
            if "wechat_ai_bot_dev" in cmd_str or "wechat_ai_bot_dev" in name or "微信ai回复_开发版" in cmd_str:
                return True
        except Exception:
            pass
        return False

    def is_bot_running(self):
        my_pid = os.getpid()
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r", encoding="utf-8") as f:
                    pid_str = f.read().strip()
                if pid_str:
                    pid = int(pid_str)
                    if pid != my_pid and psutil.pid_exists(pid):
                        p = psutil.Process(pid)
                        if self.is_target_bot_process(p):
                            return True, pid
            except Exception:
                pass
                
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if p.info['pid'] == my_pid:
                    continue
                proc = psutil.Process(p.info['pid'])
                if self.is_target_bot_process(proc):
                    return True, p.info['pid']
            except Exception:
                pass
        return False, None

    def kill_bot_processes(self):
        my_pid = os.getpid()
        # 1. Kill by PID file (带严格校验)
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r", encoding="utf-8") as f:
                    pid_str = f.read().strip()
                if pid_str:
                    pid = int(pid_str)
                    if pid != my_pid and psutil.pid_exists(pid):
                        p = psutil.Process(pid)
                        if self.is_target_bot_process(p):
                            p.kill()
            except Exception:
                pass
            try:
                os.remove(PID_FILE)
            except Exception:
                pass
                
        # 2. Kill only dev bot processes (never touch stable)
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if p.info['pid'] == my_pid:
                    continue
                proc = psutil.Process(p.info['pid'])
                if self.is_target_bot_process(proc):
                    proc.kill()
            except Exception:
                pass

    def start_bot(self):
        try:
            self.kill_bot_processes()
            time.sleep(0.3)

            self.save_ui_to_config(show_msg=False)
            
            SW_HIDE = 0
            bot_exe = os.path.join(WORKSPACE_DIR, "wechat_ai_bot_dev.exe")
            if not os.path.exists(bot_exe):
                bot_exe = os.path.join(WORKSPACE_DIR, "wechat_ai_bot.exe")
            if os.path.exists(bot_exe):
                ret = ctypes.windll.shell32.ShellExecuteW(0, "open", bot_exe, None, WORKSPACE_DIR, SW_HIDE)
            else:
                ret = ctypes.windll.shell32.ShellExecuteW(0, "open", sys.executable, f'"{BOT_SCRIPT}"', WORKSPACE_DIR, SW_HIDE)
                
            time.sleep(0.6)
            if ret > 32:
                messagebox.showinfo("启动成功", "🚀 微信 AI 自动回复守护进程已在后台成功静默启动（无黑框模式）！")
            else:
                messagebox.showerror("启动失败", f"启动守护进程失败，系统返回码: {ret}")
        except Exception as e:
            messagebox.showerror("启动失败", f"启动守护进程时出错: {e}")

    def toggle_offscreen_wechat(self):
        try:
            hwnd, _ = get_wechat_hwnd()
            if not hwnd:
                messagebox.showwarning("提示", "未检测到微信窗口！请先打开微信。")
                return
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            is_iconic = bool(user32.IsIconic(hwnd))
            
            # 只有当非系统最小化且坐标处于负数离屏区时，才判定为真正的离屏隐藏状态
            is_truly_offscreen = (not is_iconic) and (rect.left < -500 or rect.top < -500)
            
            if is_truly_offscreen:
                # 处于真正的离屏状态 -> 召回并还原到用户此前自定义的坐标与长宽
                saved = self.config_data.get("saved_window_rect", [100, 100, 1100, 750])
                x, y, w, h = saved[0], saved[1], saved[2], saved[3]
                
                screen_w = self.winfo_screenwidth()
                screen_h = self.winfo_screenheight()
                if x < 0 or x > screen_w - 200 or y < 0 or y > screen_h - 200:
                    x = max(50, int((screen_w - w) / 2))
                    y = max(50, int((screen_h - h) / 2))
                
                user32.ShowWindow(hwnd, 9) # SW_RESTORE
                user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0040 | 0x0010)
                self.btn_hide_wx.configure(text="👻 隐藏微信至离屏")
                messagebox.showinfo("已召回", f"🖥️ 微信窗口已成功还原到您设定的桌面位置！\n坐标: ({x}, {y})，尺寸: {w}x{h}")
            else:
                # 处于桌面或处于系统最小化 -> 执行隐藏！
                # 1. 若当前在桌面，原子捕获并记录当前正常位置尺寸
                if not is_iconic and rect.left >= -500 and rect.top >= -500:
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    if w >= 400 and h >= 300:
                        self.config_data["saved_window_rect"] = [int(rect.left), int(rect.top), int(w), int(h)]
                        self.save_config()
                elif is_iconic:
                    # 若处于最小化，通过 GetWindowPlacement 提取最小化前的正常尺寸
                    norm_rect = get_wechat_normal_rect(hwnd)
                    if norm_rect:
                        self.config_data["saved_window_rect"] = norm_rect
                        self.save_config()
                
                saved = self.config_data.get("saved_window_rect", [100, 100, 1100, 750])
                w, h = saved[2], saved[3]
                
                # 2. 唤醒 DWM 渲染并直接平滑移入离屏物理空间 (-3000, -3000)
                user32.ShowWindow(hwnd, 9) # SW_RESTORE
                user32.SetWindowPos(hwnd, 0, -3000, -3000, w, h, 0x0040 | 0x0010)
                self.btn_hide_wx.configure(text="🖥️ 召唤微信回桌面")
                messagebox.showinfo("已隐藏", "👻 微信已成功记录当前位置大小，并移入离屏隐形空间！\n\n您的桌面已彻底隐形不占空间，AI 守护进程依然在后台全速秒回。\n随时可点击此按钮召回微信。")
        except Exception as e:
            messagebox.showerror("错误", f"切换离屏状态失败: {e}")

    def stop_bot(self):
        try:
            self.kill_bot_processes()
            time.sleep(0.3)
            messagebox.showinfo("已停止", "⏹️ 已停止所有微信 AI 自动回复进程。")
        except Exception as e:
            messagebox.showerror("错误", f"停止失败: {e}")

    def restart_bot(self):
        self.start_bot()

    def clear_logs(self):
        try:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write("")
            self.txt_log.delete("1.0", tk.END)
        except Exception:
            pass

    def open_log_file(self):
        if os.path.exists(LOG_FILE):
            os.startfile(LOG_FILE)
        else:
            messagebox.showinfo("提示", "暂无日志文件。")

    def create_desktop_shortcut(self):
        try:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            shortcut_path = os.path.join(desktop, "微信AI自动回复.lnk")
            exe_path = os.path.join(WORKSPACE_DIR, "微信AI自动回复.exe")
            if not os.path.exists(exe_path):
                exe_path = os.path.join(WORKSPACE_DIR, "微信AI回复(开发版).exe")
            if not os.path.exists(exe_path):
                exe_path = os.path.join(WORKSPACE_DIR, "微信AI控制中心.exe")
            
            gui_script = os.path.join(WORKSPACE_DIR, "wechat_bot_gui_dev.py")
            ico_path = os.path.join(WORKSPACE_DIR, "wechat_sky_blue_unread_ai_v4.ico")
            if not os.path.exists(ico_path):
                ico_path = os.path.join(WORKSPACE_DIR, "wechat_sky_blue_v2.ico")
            if not os.path.exists(ico_path):
                ico_path = os.path.join(WORKSPACE_DIR, "wechat_sky_blue_ai_v3.ico")
            if not os.path.exists(ico_path):
                ico_path = os.path.join(WORKSPACE_DIR, "wechat_blue.ico")

            if os.path.exists(exe_path):
                target = exe_path
                args = ""
                icon = f"{exe_path},0"
            else:
                py_exe = sys.executable
                pyw_exe = py_exe.lower().replace("python.exe", "pythonw.exe")
                target = pyw_exe if os.path.exists(pyw_exe) else py_exe
                args = f'"{gui_script}"'
                icon = ico_path if os.path.exists(ico_path) else f"{target},0"

            vbs_content = f'Set WshShell = CreateObject("WScript.Shell")\n' \
                          f'Set oShortcut = WshShell.CreateShortcut("{shortcut_path}")\n' \
                          f'oShortcut.TargetPath = "{target}"\n' \
                          f'oShortcut.Arguments = {json.dumps(args)}\n' \
                          f'oShortcut.WorkingDirectory = "{WORKSPACE_DIR}"\n' \
                          f'oShortcut.IconLocation = "{icon}"\n' \
                          f'oShortcut.Description = "微信AI自动回复"\n' \
                          f'oShortcut.Save\n'
            
            tmp_vbs = os.path.join(tempfile.gettempdir(), "_tmp_sc.vbs")
            with open(tmp_vbs, "w", encoding="gbk", errors="ignore") as f:
                f.write(vbs_content)
            os.system(f'cscript //nologo "{tmp_vbs}"')
            if os.path.exists(tmp_vbs):
                try:
                    os.remove(tmp_vbs)
                except Exception:
                    pass
            messagebox.showinfo("创建成功", f"🎉 已成功在桌面创建快捷方式！\n\n快捷方式: 微信AI自动回复.lnk")
        except Exception as e:
            messagebox.showerror("错误", f"创建快捷方式失败: {e}")

    def start_background_monitors(self):
        def monitor_loop():
            while self.is_monitoring:
                # 1. Check WeChat Window (Direct Win32 API)
                try:
                    hwnd, ver = get_wechat_hwnd()
                    if hwnd:
                        wx_text = f"🟢 微信客户端: 正常连接 (WeChat {ver}.x, HWND: {hwnd})"
                        wx_color = "#107c41"
                    else:
                        wx_text = "🔴 微信客户端: 未检测到窗口 (请先登录打开微信)"
                        wx_color = "#d83b01"
                except Exception as e:
                    wx_text = f"⚪ 微信客户端: 检测中..."
                    wx_color = "#333333"

                # 2. Check Bot process
                try:
                    running, pid = self.is_bot_running()
                    if running:
                        bot_text = f"🟢 AI 守护进程: 正在运行 (PID: {pid})"
                        bot_color = "#107c41"
                    else:
                        bot_text = "⚪ AI 守护进程: 未启动"
                        bot_color = "#666666"
                except Exception:
                    bot_text = "⚪ AI 守护进程: 检测中..."
                    bot_color = "#666666"

                # 3. Read incremental logs
                new_logs = ""
                if os.path.exists(LOG_FILE):
                    try:
                        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                            f.seek(self.log_pos)
                            new_logs = f.read()
                            self.log_pos = f.tell()
                    except Exception:
                        pass

                self.after(0, lambda wt=wx_text, wc=wx_color, bt=bot_text, bc=bot_color, nl=new_logs: self._update_ui_state(wt, wc, bt, bc, nl))
                time.sleep(1.5)

        t = threading.Thread(target=monitor_loop, daemon=True)
        t.start()

    def _update_ui_state(self, wx_text, wx_color, bot_text, bot_color, new_logs):
        self.lbl_wx_status.configure(text=wx_text, foreground=wx_color)
        self.lbl_bot_status.configure(text=bot_text, foreground=bot_color)
        if new_logs:
            self.txt_log.insert(tk.END, new_logs)
            self.txt_log.see(tk.END)

if __name__ == "__main__":
    app = WeChatBotGUI()
    app.mainloop()
