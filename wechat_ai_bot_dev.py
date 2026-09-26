import os
import sys
import time
import json
import re
import hashlib
import ctypes
from ctypes import wintypes
import numpy as np
from PIL import Image, ImageGrab
import requests
import psutil
import pyperclip
from rapidocr_onnxruntime import RapidOCR

# =============================================================================
# 0. WIN32 API DEFINITIONS & DESKTOP BINDING
# =============================================================================
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shcore = ctypes.windll.shcore
kernel32 = ctypes.windll.kernel32

try:
    shcore.SetProcessDpiAwareness(2) # Per-monitor DPI aware
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

def ensure_default_desktop():
    try:
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
            user32.CloseDesktop(h_desk)
    except Exception:
        pass

ensure_default_desktop()

# =============================================================================
# 1. CONSTANTS & SYSTEM PATHS
# =============================================================================
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "wechat_config_dev.json")
CONVO_MAP_FILE = os.path.join(BASE_DIR, "wechat_convo_map.json")
PID_FILE = os.path.join(BASE_DIR, "wechat_bot_dev.pid")
LOG_FILE = os.path.join(BASE_DIR, "wechat_bot_dev.log")

try:
    if sys.stdout:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

def log(msg):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {msg}"
    try:
        print(line, flush=True)
    except Exception:
        try:
            print(line.encode("gbk", errors="ignore").decode("gbk"), flush=True)
        except Exception:
            pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def init_ocr():
    models_dir = os.path.join(BASE_DIR, "models")
    if not os.path.exists(models_dir):
        models_dir = os.path.join(BASE_DIR, "_internal", "models")
    
    det_model = os.path.join(models_dir, "ch_PP-OCRv4_det_infer.onnx")
    cls_model = os.path.join(models_dir, "ch_ppocr_mobile_v2.0_cls_infer.onnx")
    rec_model = os.path.join(models_dir, "ch_PP-OCRv4_rec_infer.onnx")
    
    if os.path.exists(det_model) and os.path.exists(rec_model):
        try:
            return RapidOCR(det_model_path=det_model, cls_model_path=cls_model, rec_model_path=rec_model)
        except Exception:
            pass
    return RapidOCR()

# Initialize local neural network OCR engine
ocr_engine = init_ocr()

# =============================================================================
# 2. CONFIGURATION & STATE MANAGEMENT
# =============================================================================
LAST_PROCESSED_SIGNATURE = {}
API_CONVERSATION_HISTORY = {}
LAST_CLICKED_CANDIDATE_TIME = {}
LAST_CHAT_HASH = None
LAST_SIDEBAR_HASH = None
LAST_FULL_SCAN_TIME = 0

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "whitelist_mode": True,
        "whitelist": ["好友A", "好友B"],
        "blacklist": ["文件传输助手", "微信团队", "订阅号", "公众号", "服务号"],
        "ai_engine": "openai_api",
        "api_url": "https://api.xiaomimimo.com/v1/chat/completions",
        "api_key": "",
        "api_model": "mimo-v2.6-flash",
        "friend_personas": {}
    }

def load_convo_map():
    if os.path.exists(CONVO_MAP_FILE):
        try:
            with open(CONVO_MAP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

# =============================================================================
# 3. DPI & PHYSICAL LAYOUT ENGINE (WINDOW-SIZE INVARIANT)
# =============================================================================
def get_dpi_scale():
    try:
        hdc = user32.GetDC(0)
        log_w = gdi32.GetDeviceCaps(hdc, 8)  # HORZRES
        phys_w = gdi32.GetDeviceCaps(hdc, 118) # DESKTOPHORZRES
        user32.ReleaseDC(0, hdc)
        scale = phys_w / float(log_w)
        return scale if scale > 0 else 1.0
    except Exception:
        return 1.0

def compute_wechat_layout(W, H):
    """
    基于微信 PC 客户端原生物理布局结构计算关键区域：
    无论窗口拉宽、拉高、缩小或最大化，物理像素绝对精准咬合。
    """
    sidebar_w = max(55, int(0.055 * W))
    chat_start_x = max(320, int(0.285 * W))
    header_h = max(85, int(0.110 * H))
    input_h = min(220, max(140, int(0.160 * H)))
    
    return {
        "sidebar_w": sidebar_w,
        "chat_start_x": chat_start_x,
        "header_h": header_h,
        "input_h": input_h,
        "total_w_px": W,
        "total_h_px": H
    }

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)

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

def grab_wechat_window(hwnd):
    """
    开发版后台静默离屏抓图引擎（PrintWindow / DWM）：
    无论微信被其他窗口覆盖压在底层还是处于离屏隐形状态，都能静默抓取微信自身的 100% 真实画面！
    """
    try:
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            w, h = 1100, 750
            
        scale = get_dpi_scale()
        
        # 1. 优先尝试 Win32 PrintWindow 纯后台截取 (PW_RENDERFULLCONTENT = 2)
        hwnd_dc = user32.GetWindowDC(hwnd)
        mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        save_bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        gdi32.SelectObject(mfc_dc, save_bitmap)
        
        result = user32.PrintWindow(hwnd, mfc_dc, 2)
        if not result:
            result = user32.PrintWindow(hwnd, mfc_dc, 0)
            
        img = None
        if result:
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ('biSize', wintypes.DWORD),
                    ('biWidth', wintypes.LONG),
                    ('biHeight', wintypes.LONG),
                    ('biPlanes', wintypes.WORD),
                    ('biBitCount', wintypes.WORD),
                    ('biCompression', wintypes.DWORD),
                    ('biSizeImage', wintypes.DWORD),
                    ('biXPelsPerMeter', wintypes.LONG),
                    ('biYPelsPerMeter', wintypes.LONG),
                    ('biClrUsed', wintypes.DWORD),
                    ('biClrImportant', wintypes.DWORD)
                ]
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0
            
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mfc_dc, save_bitmap, 0, h, buf, ctypes.byref(bmi), 0)
            img = Image.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1).convert('RGB')
            
        gdi32.DeleteObject(save_bitmap)
        gdi32.DeleteDC(mfc_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
        
        if img and np.array(img).max() > 10:
            return img, rect, scale
        else:
            # 备用：物理截屏 (仅当在正常屏幕内时)
            if rect.left > -1000 and rect.top > -1000:
                bbox = (int(rect.left * scale), int(rect.top * scale), int(rect.right * scale), int(rect.bottom * scale))
                fallback_img = ImageGrab.grab(bbox=bbox).convert('RGB')
                return fallback_img, rect, scale
            return img, rect, scale
    except Exception:
        return None, None, 1.0

def activate_wechat(hwnd):
    """开发版静默就绪引擎：0 弹窗 0 置顶 0 焦点抢占"""
    pass

def post_click_client_point(hwnd, client_x, client_y):
    """
    通过 Win32 消息向微信窗口直接发送局部点击消息，
    绝对不调用 SetCursorPos，物理鼠标 1 个像素都不会动！
    """
    try:
        lParam = (int(client_y) << 16) | (int(client_x) & 0xFFFF)
        user32.PostMessageW(hwnd, 0x0201, 0x0001, lParam) # WM_LBUTTONDOWN
        time.sleep(0.02)
        user32.PostMessageW(hwnd, 0x0202, 0x0000, lParam) # WM_LBUTTONUP
        time.sleep(0.03)
    except Exception:
        pass

def click_screen_point(x, y):
    """开发版兼容保留函数"""
    pass

# =============================================================================
# 4. HIGH-PERFORMANCE ROBUST NAME MATCHING & RED BADGE FILTER
# =============================================================================
def normalize_for_match(text):
    if not text:
        return ""
    t = str(text).lower().strip()
    t = re.sub(r'\s+', '', t)
    t = t.replace('0', 'o').replace('1', 'l').replace('i', 'l')
    t_clean = re.sub(r'[\^_\~\-\.\,\'\"\`\*\#\@\!\:\;\|\/\\]', '', t)
    return t_clean

def is_name_matched_strict(contact_name, whitelist):
    if not contact_name or len(str(contact_name).strip()) < 2:
        return False, ""
        
    c_raw = str(contact_name).strip()
    # 群聊与非白名单防火墙：拦截一切带人数后缀群或群聊关键字
    if re.search(r'\(\d+\)$|\[\d+条\]|\b群\b|VIP|社区|福利', c_raw):
        return False, ""
        
    norm_ocr = normalize_for_match(c_raw)
    
    for t in whitelist:
        t_clean = t.strip()
        if not t_clean:
            continue
            
        norm_w = normalize_for_match(t_clean)
        # 1. 严格全等
        if norm_ocr == norm_w:
            return True, t_clean
        # 2. 前缀精准匹配（例如 "张三测试" 只要以 "张三" 开头即 100% 命中）
        if norm_ocr.startswith(norm_w):
            return True, t_clean
        # 3. 包含关系与双向模糊容错（支持后面跟随较长消息预览）
        if norm_w in norm_ocr and len(norm_ocr) <= len(norm_w) + 20:
            return True, t_clean
        if norm_ocr in norm_w and len(norm_w) <= len(norm_ocr) + 3:
            return True, t_clean
            
    return False, ""

def has_red_badge_by_text_anchor(arr, text_box, layout):
    """
    【高精度动态几何锚定红点算法】：
    以 OCR 识别出的好友名字文字 Box 为基准，自适应覆盖头像右上角红点与数字红圈区域！
    """
    total_w = layout["total_w_px"]
    total_h = layout["total_h_px"]
    
    # 获取联系人文字左边缘与顶部坐标
    txt_left_x = min(pt[0] for pt in text_box)
    txt_top_y = min(pt[1] for pt in text_box)
    
    # 拓宽头像右上角红点与数字红圈采样空间 (txt_left_x - 36 ~ txt_left_x - 3)
    x1 = max(0, int(txt_left_x - 36))
    x2 = max(0, int(txt_left_x - 3))
    y1 = max(0, int(txt_top_y - 20))
    y2 = min(total_h, int(txt_top_y + 20))
    
    patch = arr[y1:y2, x1:x2]
    if patch.size == 0:
        return False
        
    # 微信原生高纯度亮红色判定 (红像素点数 >= 15，转 int 彻底杜绝 uint8 回绕溢出)
    pr, pg, pb = patch[:, :, 0].astype(int), patch[:, :, 1].astype(int), patch[:, :, 2].astype(int)
    red_mask = (pr >= 200) & (pg <= 110) & (pb <= 110) & (pr > pg + 65)
    return np.sum(red_mask) >= 15

def extract_active_chat_title(res, layout):
    """
    顶栏好友昵称提取（Window-Size Invariant）：
    严格锁定在 X >= chat_start_x 且 Y <= header_h，绝不下沉到聊天气泡区。
    """
    chat_start_x = layout["chat_start_x"]
    header_h = layout["header_h"]
    total_w = layout["total_w_px"]
    
    title_parts = []
    for b, txt, sc in (res or []):
        cx = (b[0][0] + b[1][0]) / 2.0
        cy = (b[0][1] + b[2][1]) / 2.0
        t_clean = txt.strip()
        
        # 严格限制在顶栏左侧区域（排除右上角窗口控制按钮与杂字）
        if (chat_start_x - 30) <= cx <= (chat_start_x + int(0.42 * (total_w - chat_start_x))) and cy <= header_h:
            if t_clean in ["最小化", "最大化", "关闭", "设置", "聊天信息", "搜索", "表情", "发送", "文件", "截图", "语音聊天", "视频聊天", "…", "...", "口", "-", "x", "X"]:
                continue
            title_parts.append(t_clean)
            
    return " ".join(title_parts)

# =============================================================================
# 5. CHROMATIC BUBBLE PARSER & MULTI-LINE AGGREGATION
# =============================================================================
BUILTIN_IGNORED_KEYWORDS = [
    "按住鼠标", "语音输入文字", "按住说话", "按Enter发送", "按Ctrl+Enter发送", "发送(S)"
]

def parse_chat_bubbles_chromatic(res, img_rgb, layout, ignored_keywords=None):
    arr = np.array(img_rgb)
    W, H = img_rgb.size
    chat_start_x = layout["chat_start_x"]
    header_h = layout["header_h"]
    input_h = layout["input_h"]
    
    # 汇总系统内置提示词 + 用户自定义屏蔽词
    all_ignored = list(BUILTIN_IGNORED_KEYWORDS)
    if ignored_keywords and isinstance(ignored_keywords, list):
        for kw in ignored_keywords:
            if kw and str(kw).strip() and str(kw).strip() not in all_ignored:
                all_ignored.append(str(kw).strip())
    
    raw_lines = []
    for b, txt, sc in (res or []):
        cx = (b[0][0] + b[1][0]) / 2.0
        cy = (b[0][1] + b[2][1]) / 2.0
        c_clean = txt.strip()
        if not c_clean:
            continue
            
        # 排除系统原生 UI 提示词与屏蔽词（如鼠标悬停输入框时弹出的“按住鼠标 语音输入文字”等）
        if any(bad in c_clean for bad in all_ignored):
            continue
            
        # 排除顶栏乱码与非聊天文字 (纯数字如 666, 1, 520, 21 等 100% 完整保留支持)
        if any(bad in c_clean for bad in ["P以H白", "y< o O", "查看更多", "以下为新消息", "置顶聊天"]):
            continue
        
        # 严格限定在主聊天气泡区域 (顶部避开标题栏 + 15px，底部避开输入工具栏)
        if cx > (chat_start_x + 15) and (header_h + 15) < cy < (H - int(input_h * 0.70)):
            if re.match(r'^\d{1,2}:\d{2}$', c_clean) or re.match(r'^\d{1,2}/\d{2}$', c_clean) or re.match(r'^\d{4}-\d{2}-\d{2}', c_clean):
                continue
            if c_clean in ["昨天", "前天", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日", "查看更多消息", "重新编辑", "发送"]:
                continue
            if any(c_clean.startswith(p) for p in ["[转账]", "微信转账", "已收款", "已被接收", "￥", "你撤回了一条消息"]):
                continue
            if "拍了拍" in c_clean:
                continue
                
            # 采样气泡底色 (外扩采样)
            min_x = max(0, int(min(pt[0] for pt in b) - 15))
            max_x = min(W, int(max(pt[0] for pt in b) + 15))
            min_y = max(0, int(min(pt[1] for pt in b) - 8))
            max_y = min(H, int(max(pt[1] for pt in b) + 8))
            
            patch = arr[min_y:max_y, min_x:max_x]
            pr, pg, pb = patch[:, :, 0].astype(int), patch[:, :, 1].astype(int), patch[:, :, 2].astype(int)
            green_mask = (pg > pr + 18) & (pg > pb + 18) & (pg > 45)
            has_green_bg = np.sum(green_mask) > 15
            
            box_left = min(pt[0] for pt in b)
            box_right = max(pt[0] for pt in b)
            
            # 真彩底色判定：绿底 100% 我发，非绿底偏左侧 100% 好友发
            is_incoming = (not has_green_bg) and (box_left < (chat_start_x + int(0.55 * (W - chat_start_x))))
            raw_lines.append((cy, box_left, box_right, is_incoming, c_clean))
            
    raw_lines.sort(key=lambda x: x[0])
    
    # 智能多行气泡垂直合并 (< 40px)
    merged_bubbles = []
    for cy, b_left, b_right, is_inc, text in raw_lines:
        if merged_bubbles and is_inc == merged_bubbles[-1]['is_incoming'] and abs(cy - merged_bubbles[-1]['last_y']) < 40:
            merged_bubbles[-1]['text'] += "，" + text
            merged_bubbles[-1]['last_y'] = cy
        else:
            merged_bubbles.append({
                'is_incoming': is_inc,
                'first_y': cy,
                'last_y': cy,
                'text': text
            })
            
    return merged_bubbles

def extract_pending_incoming_messages(chat_bubbles):
    """
    夹层未读穿透回溯算法 (Bubble Penetration Scanner):
    寻找最后一条我方绿底气泡 (last_outgoing_idx)，
    将其下方所有的好友连发气泡全部完整聚合提取！
    """
    if not chat_bubbles:
        return [], ""
        
    last_outgoing_idx = -1
    for i in range(len(chat_bubbles) - 1, -1, -1):
        if not chat_bubbles[i]['is_incoming']:
            last_outgoing_idx = i
            break
            
    if last_outgoing_idx == -1:
        pending_bubbles = [b for b in chat_bubbles if b['is_incoming']]
    else:
        pending_bubbles = [b for b in chat_bubbles[last_outgoing_idx + 1:] if b['is_incoming']]
        
    texts = [b['text'].strip() for b in pending_bubbles if b['text'].strip()]
    combined_text = "，".join(texts)
    return pending_bubbles, combined_text

# =============================================================================
# 6. LLM BRAIN & INSTANT PURE TEXT GENERATION (0 ARTIFICIAL LATENCY)
# =============================================================================
def clean_ai_reply(reply):
    if not reply:
        return ""
    reply = re.sub(r'<think>.*?</think>', '', reply, flags=re.DOTALL)
    reply = re.sub(r'^[（(][^）)]*[）)]\s*', '', reply)
    reply = re.sub(r'\s*[（(][^）)]*[）)]$', '', reply)
    reply = re.sub(r'[*_#`]', '', reply)
    reply = reply.strip()
    if reply.startswith(('"', '“')) and reply.endswith(('"', '”')):
        reply = reply[1:-1].strip()
    return reply

def generate_ai_reply(sender, message_content):
    cfg = load_config()
    now_time = time.strftime("%H:%M")
    persona_desc = cfg.get("friend_personas", {}).get(sender, "幽默风趣、随和自然的真实好友")
    
    system_prompt = (
        f"你现在是微信账号的主人本人（真实真人），正在微信上与好友【{sender}】聊天。当前时间是{now_time}。\n"
        f"【专属人设风格】：{persona_desc}\n"
        "【对话规则】：直接输出发给对方的一句中文，口语化自然、简练得体。严禁括号描写心理活动，严禁透露AI身份。"
    )
    
    oa = cfg.get("openai_api", {})
    api_base = cfg.get("api_url") or oa.get("api_base", "https://api.xiaomimimo.com/v1")
    url = api_base if api_base.endswith("/chat/completions") else f"{api_base.rstrip('/')}/chat/completions"
    key = cfg.get("api_key") or oa.get("api_key", "")
    model = cfg.get("api_model") or oa.get("model", "mimo-v2.6-flash")
    
    if key and url:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        history = API_CONVERSATION_HISTORY.get(sender, [])
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-8:])
        messages.append({"role": "user", "content": message_content})
        
        payload = {"model": model, "messages": messages, "temperature": 0.7}
        try:
            log(f"🤖 正在调用大模型 ({model}) 回复【{sender}】...")
            res = requests.post(url, headers=headers, json=payload, timeout=12)
            if res.status_code == 200:
                reply = res.json()["choices"][0]["message"]["content"]
                cleaned = clean_ai_reply(reply)
                history.append({"role": "user", "content": message_content})
                history.append({"role": "assistant", "content": cleaned})
                API_CONVERSATION_HISTORY[sender] = history[-10:]
                return cleaned
            else:
                log(f"API HTTP {res.status_code}: {res.text[:100]}")
        except Exception as e:
            log(f"API Error: {e}")
            
    return f"在呢，刚才没注意看手机，怎么啦？"

def verify_outgoing_bubble_success(hwnd, layout):
    """
    发送后真彩绿底闭环验收引擎：
    在回车发送后截取聊天区底部，校验是否真正长出了微信特征绿底 (#95EC69) 气泡！
    """
    try:
        time.sleep(0.22)
        img_verify, _, _ = grab_wechat_window(hwnd)
        if not img_verify:
            return False
            
        W, H = img_verify.size
        arr_v = np.array(img_verify)
        chat_start_x = layout["chat_start_x"]
        input_h = layout["input_h"]
        
        # 采样聊天视窗底部最后 220px 区域 (即最新发出的气泡所在的右侧区域)
        y1 = max(0, int(H - input_h - 220))
        y2 = min(H, int(H - input_h + 15))
        x1 = int(chat_start_x + 0.20 * (W - chat_start_x)) # 偏右侧 (我发绿底气泡区域)
        x2 = min(W, int(W - 10))
        
        patch = arr_v[y1:y2, x1:x2]
        if patch.size == 0:
            return False
            
        # 微信特征绿底色判定 (转 int 杜绝浅色背景回绕溢出)
        pr, pg, pb = patch[:, :, 0].astype(int), patch[:, :, 1].astype(int), patch[:, :, 2].astype(int)
        green_mask = (pg > pr + 18) & (pg > pb + 18) & (pg > 45)
        return np.sum(green_mask) >= 18
    except Exception:
        return False

def safe_clipboard_copy(text, retries=3, delay=0.03):
    """
    带冲突退避重试的剪贴板安全写入函数：
    有效化解 Windows 剪贴板历史 (Win+V)、杀软或浏览器占用导致的 OpenClipboard 冲突。
    """
    for i in range(retries):
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            if i < retries - 1:
                time.sleep(delay)
    return False

def send_reply_instant(hwnd, rect, layout, target, final_reply):
    """
    极速秒回直达引擎（局部消息投递，0 弹窗 0 鼠标物理移动）：
    1. 预检当前顶栏目标
    2. 若目标偏离，自动在左侧重定向锁定
    3. 若无法定位目标，触发安全熔断，绝不串发
    4. 线程挂接输入 -> 局部消息注入 -> 真彩绿底闭环验收
    5. 极速就地完成
    """
    activate_wechat(hwnd)
    
    # --- 发送前核验 (Pre-Flight Verification) ---
    img_now, cur_rect, cur_scale = grab_wechat_window(hwnd)
    if not img_now:
        log(f"⚠️ [发送前核验失败] 无法获取微信画面，终止发送防误触")
        return False
        
    cur_W, cur_H = img_now.size
    cur_layout = compute_wechat_layout(cur_W, cur_H)
    
    # 极速轻量 OCR 顶栏与左侧列表
    target_w = 1000
    downscale_ratio = 1000.0 / cur_W if cur_W > 1100 else 1.0
    if downscale_ratio < 1.0:
        img_for_ocr = img_now.resize((target_w, int(cur_H * downscale_ratio)), Image.Resampling.BILINEAR)
        res_raw, _ = ocr_engine(img_for_ocr)
        res_now = []
        for b, txt, sc in (res_raw or []):
            mapped_box = [[pt[0] / downscale_ratio, pt[1] / downscale_ratio] for pt in b]
            res_now.append((mapped_box, txt, sc))
    else:
        res_now, _ = ocr_engine(img_now)
        
    cur_title = extract_active_chat_title(res_now, cur_layout)
    is_right_target = is_name_matched_strict(cur_title, [target])[0] if cur_title else False
    
    # 若顶栏不是目标好友，触发智能重定向
    if not is_right_target:
        log(f"⚠️ [发送前纠偏] 检测到窗口偏离（当前为【{cur_title or '非目标'}】），正在重定向锁定【{target}】...")
        found_cand_y = None
        sidebar_w = cur_layout["sidebar_w"]
        chat_start_x = cur_layout["chat_start_x"]
        for b, txt, sc in (res_now or []):
            cx = (b[0][0] + b[1][0]) / 2.0
            cy = (b[0][1] + b[2][1]) / 2.0
            if sidebar_w <= cx <= chat_start_x and (cur_layout["header_h"] + 5) <= cy <= (cur_H - 20):
                if is_name_matched_strict(txt.strip(), [target])[0]:
                    found_cand_y = cy
                    break
                    
        if found_cand_y is not None:
            post_click_client_point(hwnd, int(0.16 * cur_W), int(found_cand_y))
            time.sleep(0.10)
            log(f"🎯 [后台静默纠偏] 已重新切入【{target}】聊天视窗")
        else:
            log(f"🚫 [安全熔断] 未在列表中定位到目标【{target}】，放弃本次发送以防串发！")
            return False
            
    log(f"📤 正在回复【{target}】: {final_reply}")
    
    # 剪贴板原子快照保护
    user_old_clip = None
    try:
        user_old_clip = pyperclip.paste()
    except Exception:
        pass
        
    user_orig_hwnd = user32.GetForegroundWindow()
    send_success = False
    for attempt in range(1, 3):
        # 1. 局部消息模拟点击输入框聚焦 (物理鼠标 0 像素移动)
        post_click_client_point(hwnd, int(0.55 * cur_W), int(cur_H - 0.08 * cur_H))
        time.sleep(0.02)
        
        # 2. 剪贴板填充真实完整文本 (带防冲突退避重试)
        if not safe_clipboard_copy(final_reply, retries=3, delay=0.03):
            log(f"⚠️ [剪贴板写入受阻(尝试 {attempt}/2)] 系统剪贴板正被其他程序独占，稍后重试...")
            time.sleep(0.05)
            continue
        time.sleep(0.02)
        
        # 3. 极速瞬态激活注入 (15ms 瞬态，绝不挪动鼠标)
        cur_thread = user32.GetWindowThreadProcessId(user_orig_hwnd, None) if user_orig_hwnd else 0
        wx_thread = user32.GetWindowThreadProcessId(hwnd, None)
        my_thread = kernel32.GetCurrentThreadId()
        
        attached_cur = False
        attached_my = False
        try:
            if cur_thread and cur_thread != wx_thread:
                attached_cur = bool(user32.AttachThreadInput(cur_thread, wx_thread, True))
            if my_thread and my_thread != wx_thread:
                attached_my = bool(user32.AttachThreadInput(my_thread, wx_thread, True))
                
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.01)
            
            # 发送真实的 Ctrl+V 组合键 (彻底杜绝误发单字母 v)
            user32.keybd_event(0x11, 0, 0, 0)
            user32.keybd_event(0x56, 0, 0, 0)
            time.sleep(0.01)
            user32.keybd_event(0x56, 0, 0x0002, 0)
            user32.keybd_event(0x11, 0, 0x0002, 0)
            time.sleep(0.02)
            
            # 回车发送
            user32.keybd_event(0x0D, 0, 0, 0)
            time.sleep(0.01)
            user32.keybd_event(0x0D, 0, 0x0002, 0)
            time.sleep(0.03)
            
            # 4. 瞬间恢复用户原本的窗口层级 (若微信原本在底层/后台，立即压回底层并恢复用户前台)
            if user_orig_hwnd and user_orig_hwnd != hwnd:
                user32.SetForegroundWindow(user_orig_hwnd)
                user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0003 | 0x0010) # HWND_BOTTOM, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        finally:
            if attached_cur:
                user32.AttachThreadInput(cur_thread, wx_thread, False)
            if attached_my:
                user32.AttachThreadInput(my_thread, wx_thread, False)
            
        # 5. 🔍 真彩绿底闭环验收
        if verify_outgoing_bubble_success(hwnd, cur_layout):
            send_success = True
            break
        else:
            log(f"⚠️ [发送闭环验收未通过(尝试 {attempt}/2)] 未检测到新绿底气泡，正在执行强力二次重试...")
            time.sleep(0.10)
    
    # 还原用户原本的剪贴板 (带安全重试)
    if user_old_clip is not None:
        safe_clipboard_copy(user_old_clip, retries=2, delay=0.02)
            
    if send_success:
        log(f"✅ [后台静默秒回成功] 已回复【{target}】！(物理鼠标 0 移动，窗口层级已锁定)")
    else:
        log(f"❌ [发送脱靶告警] 重试后仍未检测到绿底气泡！")
    
    return send_success

# =============================================================================
# 7. 左右双轨协同感知引擎 (DUAL-TRACK COLLABORATIVE ENGINE)
# =============================================================================
def scan_and_reply_wechat(hwnd):
    global LAST_CHAT_HASH, LAST_SIDEBAR_HASH, LAST_FULL_SCAN_TIME
    ensure_default_desktop()
    cfg = load_config()
    
    img_wechat, rect, scale = grab_wechat_window(hwnd)
    if not img_wechat:
        return
        
    W, H = img_wechat.size
    if W <= 100 or H <= 100:
        return
        
    layout = compute_wechat_layout(W, H)
    arr_wechat = np.array(img_wechat)
    
    # -------------------------------------------------------------------------
    # ⚡ 像素级动态感知哨兵 (Pixel Sentinel: 20ms 极速变动感知，消灭 18s OCR 空转)
    # -------------------------------------------------------------------------
    crop_x1 = max(330, int(layout["chat_start_x"] + 10))
    crop_y1 = max(0, int(layout["header_h"] + 10))
    crop_x2 = min(W, int(W - 5))
    crop_y2 = min(H, int(H - int(layout["input_h"] * 0.70)))
    
    chat_patch = arr_wechat[crop_y1:crop_y2, crop_x1:crop_x2]
    curr_chat_hash = hashlib.md5(chat_patch.tobytes()).hexdigest()
    
    sidebar_patch = arr_wechat[:, :layout["chat_start_x"]]
    sr, sg, sb = sidebar_patch[:, :, 0].astype(int), sidebar_patch[:, :, 1].astype(int), sidebar_patch[:, :, 2].astype(int)
    red_mask = (sr >= 190) & (sg <= 115) & (sb <= 115) & (sr > sg + 55)
    has_red_pixels = np.sum(red_mask) >= 15
    
    curr_sidebar_hash = hashlib.md5(sidebar_patch.tobytes()).hexdigest()
    now_ts = time.time()
    
    # 若画面未发生任何像素改变、无红点且距离上次扫描 < 1.0 秒：直接 20ms 极速放行！
    if curr_chat_hash == LAST_CHAT_HASH and curr_sidebar_hash == LAST_SIDEBAR_HASH and not has_red_pixels and (now_ts - LAST_FULL_SCAN_TIME) < 1.0:
        return
        
    LAST_CHAT_HASH = curr_chat_hash
    LAST_SIDEBAR_HASH = curr_sidebar_hash
    LAST_FULL_SCAN_TIME = now_ts
    
    # 动态下采样加速 OCR (等比下采样至 1000 宽，耗时从 5.5s 降至 0.8s)
    target_w = 1000
    downscale_ratio = 1000.0 / W if W > 1100 else 1.0
    if downscale_ratio < 1.0:
        target_h = int(H * downscale_ratio)
        img_for_ocr = img_wechat.resize((target_w, target_h), Image.Resampling.BILINEAR)
        res_raw, _ = ocr_engine(img_for_ocr)
        res = []
        for b, txt, sc in (res_raw or []):
            mapped_box = [[pt[0] / downscale_ratio, pt[1] / downscale_ratio] for pt in b]
            res.append((mapped_box, txt, sc))
    else:
        res, _ = ocr_engine(img_wechat)
        
    if not res:
        return
        
    whitelist_mode = cfg.get("whitelist_mode", True)
    whitelist = cfg.get("whitelist", [])
    blacklist = cfg.get("blacklist", [])
    ignored_keywords = cfg.get("ignored_keywords", [])
    
    sidebar_w = layout["sidebar_w"]
    chat_start_x = layout["chat_start_x"]
    
    # -------------------------------------------------------------------------
    # 轨 1: 左侧会话全局看板 (提取高浓度绿色激活项 & 红点列表)
    # -------------------------------------------------------------------------
    session_items = []
    for b, txt, sc in res:
        cx = (b[0][0] + b[1][0]) / 2.0
        cy = (b[0][1] + b[2][1]) / 2.0
        if sidebar_w <= cx <= chat_start_x and (layout["header_h"] + 5) <= cy <= (layout["total_h_px"] - 20):
            session_items.append((cy, cx, txt.strip(), b))
            
    session_items.sort(key=lambda x: x[0])
    
    active_green_text = None
    max_green_count = 0
    red_badge_candidates = []
    
    for cy, cx, text_clean, b in session_items:
        if any(b_name in text_clean for b_name in blacklist if b_name.strip()):
            continue
            
        # 统计该行绿色像素强度 (寻找真正被激活的会话，转 int 杜绝浅色背景回绕溢出)
        min_y = max(0, int(cy - 18))
        max_y = min(H, int(cy + 18))
        patch = arr_wechat[min_y:max_y, sidebar_w:chat_start_x]
        pr, pg, pb = patch[:, :, 0].astype(int), patch[:, :, 1].astype(int), patch[:, :, 2].astype(int)
        green_pixels = np.sum((pg > pr + 25) & (pg > pb + 25) & (pg > 80))
        
        if green_pixels > 1200 and green_pixels > max_green_count:
            max_green_count = green_pixels
            active_green_text = text_clean
            
        # 判定是否有新消息红点 (基于文字动态几何锚定)
        matched, target = is_name_matched_strict(text_clean, whitelist) if whitelist_mode else (len(text_clean) >= 2 and text_clean not in ["搜索", "微信", "朋友圈"], text_clean)
        if matched and has_red_badge_by_text_anchor(arr_wechat, b, layout):
            red_badge_candidates.append((cy, target))
                
    # -------------------------------------------------------------------------
    # 轨 2: 右侧微观聊天区 (提取顶栏标题 & 消息气泡)
    # -------------------------------------------------------------------------
    active_right_title = extract_active_chat_title(res, layout)
    
    # -------------------------------------------------------------------------
    # 🌟 白名单绝对硬核门禁 (Strict Whitelist Gating: 绝不误回复非白名单人员)
    # -------------------------------------------------------------------------
    current_active_target = None
    
    # 1. 优先校验右侧顶栏标题
    if active_right_title:
        matched_right, right_target = is_name_matched_strict(active_right_title, whitelist)
        if matched_right:
            current_active_target = right_target
            
    # 2. 双重保险：若顶栏未匹配上，但左侧选中的主激活绿条明确属于白名单好友
    if not current_active_target and active_green_text:
        matched_left, left_target = is_name_matched_strict(active_green_text, whitelist)
        if matched_left:
            current_active_target = left_target
    
    if current_active_target:
        chat_bubbles = parse_chat_bubbles_chromatic(res, img_wechat, layout, ignored_keywords)
        if chat_bubbles:
            pending_bubbles, combined_text = extract_pending_incoming_messages(chat_bubbles)
            # 🔥 第一道铁闸：只要当前好友有未回复新消息，绝对锁定在当前窗口就地秒回！
            if pending_bubbles and combined_text:
                current_sig = (current_active_target, combined_text, int(pending_bubbles[-1]['last_y']), len(chat_bubbles))
                if LAST_PROCESSED_SIGNATURE.get(current_active_target) != current_sig:
                    
                    # 🌟 极速动态像素沉降判定 (Dynamic Pixel Settling):
                    # 短暂等待 0.35 秒 (人类打字发句最小停顿)，若右侧气泡像素无变动，立即判定单句发送完毕，0秒多余等待直接交由 AI！
                    # 若检测到右侧像素哈希发生改变（连发新气泡），则等待 0.15 秒排版沉降后增量 OCR 聚合全部短句！
                    time.sleep(0.35)
                    img_latest, _, _ = grab_wechat_window(hwnd)
                    if img_latest:
                        latest_patch = np.array(img_latest)[crop_y1:crop_y2, crop_x1:crop_x2]
                        new_chat_hash = hashlib.md5(latest_patch.tobytes()).hexdigest()
                        if new_chat_hash != curr_chat_hash:
                            time.sleep(0.15) # 等待微信 Qt5 文字渲染排版彻底完成
                            img_settled, _, _ = grab_wechat_window(hwnd)
                            if img_settled:
                                img_chat_crop_lat = img_settled.crop((crop_x1, crop_y1, crop_x2, crop_y2))
                                res_lat_raw, _ = ocr_engine(img_chat_crop_lat)
                                res_lat = []
                                for b, txt, sc in (res_lat_raw or []):
                                    res_lat.append(([[pt[0] + crop_x1, pt[1] + crop_y1] for pt in b], txt, sc))
                                chat_bubbles_lat = parse_chat_bubbles_chromatic(res_lat, img_settled, layout, ignored_keywords)
                                pending_lat, combined_lat = extract_pending_incoming_messages(chat_bubbles_lat)
                                if pending_lat and combined_lat:
                                    pending_bubbles = pending_lat
                                    combined_text = combined_lat
                                    current_sig = (current_active_target, combined_text, int(pending_bubbles[-1]['last_y']), len(chat_bubbles_lat))
                            
                    LAST_PROCESSED_SIGNATURE[current_active_target] = current_sig
                    
                    log(f"\n📩 [当前会话锁定·连发聚合] 收到当前好友【{current_active_target}】新消息({len(pending_bubbles)}条): \"{combined_text}\"")
                    ai_reply = generate_ai_reply(current_active_target, combined_text)
                    if ai_reply:
                        prefix = cfg.get("reply_prefix", "") if cfg.get("include_prefix", False) else ""
                        final_reply = f"{prefix}{ai_reply}"
                        send_reply_instant(hwnd, rect, layout, current_active_target, final_reply)
                
                # 只要当前会话存在待回复/刚回复的消息，直接 return，100% 物理阻断后续任何红点跳转！
                return
            elif chat_bubbles[-1]['is_incoming']:
                # 即使最新一条消息处于特殊状态，只要属于好友发出，绝对坚守当前窗口！
                return

    # -------------------------------------------------------------------------
    # 协同通道 B: 只有确认当前会话已彻底回完(最新为我发出的绿底)，才极速切入其他白名单好友红点
    # -------------------------------------------------------------------------
    if red_badge_candidates:
        now_ts = time.time()
        valid_candidates = []
        for cand_y, cand_target in red_badge_candidates:
            if current_active_target and cand_target == current_active_target:
                continue
            if active_right_title and is_name_matched_strict(active_right_title, [cand_target])[0]:
                continue
            # 防空转冷却：若 3 秒内刚点击过该联系人且未产生新回复，跳过防死循环
            if cand_target in LAST_CLICKED_CANDIDATE_TIME and (now_ts - LAST_CLICKED_CANDIDATE_TIME[cand_target]) < 3.0:
                continue
            valid_candidates.append((cand_y, cand_target))
            
        if valid_candidates:
            cand_y, cand_target = valid_candidates[0]
            LAST_CLICKED_CANDIDATE_TIME[cand_target] = now_ts
            log(f"\n🔔 [当前已回完·极速切入] 发现白名单好友【{cand_target}】未读红点，正在锁定切入...")
            activate_wechat(hwnd)
            
            # 点击左侧联系人项目中心位置 (sidebar_w + 0.45*(chat_start_x - sidebar_w), cand_y)
            click_x = int(layout["sidebar_w"] + 0.45 * (layout["chat_start_x"] - layout["sidebar_w"]))
            post_click_client_point(hwnd, click_x, int(cand_y))
            time.sleep(0.35) # 充分等待 Qt5 引擎将右侧所有新消息气泡 100% 渲染绘制完成
            
            # 强制重置像素哨兵哈希，确保切入后 100% 触发全量 OCR 读取
            LAST_CHAT_HASH = None
            LAST_SIDEBAR_HASH = None
            LAST_FULL_SCAN_TIME = 0
            
            # 立即触发就地扫描，无需空等下个心跳周期，彻底消灭切换延迟
            scan_and_reply_wechat(hwnd)
            return

# =============================================================================
# 8. DAEMON MAIN LOOP
# =============================================================================
def main_loop():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
        
    log("=" * 60)
    log("🚀 PC 微信真人拟真 AI 自动回复系统【左右双轨协同感知版】已启动")
    log("   左轨看板: 绿色激活项背景判定 + 宽容度红点巡检")
    log("   右轨微观: 顶栏标题交叉锚定 + 贴底气泡真彩流向分析")
    log("   协同仲裁: 三元协同指纹去重，绝不漏掉任何短消息与标点")
    log("=" * 60)
    
    while True:
        try:
            hwnd, ver = get_wechat_hwnd()
            if hwnd:
                scan_and_reply_wechat(hwnd)
            cfg = load_config()
            interval = float(cfg.get("check_interval_seconds", 0.8))
            time.sleep(interval)
        except Exception as e:
            time.sleep(1.0)

if __name__ == "__main__":
    main_loop()
