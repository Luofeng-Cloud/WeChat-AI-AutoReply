import os
import io
import re
import time
import base64
import hashlib
import ctypes
from ctypes import wintypes
from collections import deque
import numpy as np
from PIL import Image, ImageGrab

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)

# 匹配微信语音气泡时长文字，如: 3"  12"  5”  8''  15′
VOICE_DURATION_RE = re.compile(r'^\s*(\d{1,2})\s*["”\'’″′]{1,2}\s*$')

# 微信语音转文字失败时的常见系统提示
VOICE_FAIL_HINTS = ("无法识别语音内容", "转换失败", "语音转换失败", "未识别到语音")

# 系统卡片关键字（避免把转账/文件卡片当普通照片发给视觉模型）
SYSTEM_CARD_HINTS = ("微信转账", "已收款", "已被接收", "撤回了一条消息", "微信红包", "领取红包")


# =============================================================================
# 1. 图像压缩编码与感知指纹 (供后台与 GUI 沙盒共用)
# =============================================================================
def compute_image_perceptual_hash(pil_img):
    """计算 16x16 灰度降采样块均值哈希，对 1~2px 渲染抖动鲁棒，用于图片消息去重签名"""
    if pil_img is None:
        return ""
    small = pil_img.convert("L").resize((16, 16), Image.Resampling.BILINEAR)
    arr = np.array(small, dtype=np.float32)
    # 量化到 16 级灰度再哈希，既保留画面区分度又抗轻微抗锯齿差异
    quant = (arr // 16).astype(np.uint8).tobytes()
    return hashlib.md5(quant).hexdigest()[:12]


def encode_pil_to_data_url(pil_img, max_side=768, quality=85):
    """将 PIL 图像等比缩放并压缩为标准 data:image/jpeg;base64,... 字符串"""
    if pil_img is None:
        return "", ""
    img = pil_img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        ratio = max_side / float(max(w, h))
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    img_hash = compute_image_perceptual_hash(pil_img)
    return f"data:image/jpeg;base64,{b64}", img_hash


def encode_image_file_to_data_url(file_path, max_side=768, quality=85):
    """读取本地图片文件转为 OpenAI Vision 可用的 data_url（供测试沙盒使用）"""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"找不到图片文件: {file_path}")
    with Image.open(file_path) as im:
        return encode_pil_to_data_url(im, max_side=max_side, quality=quality)


def build_multimodal_user_content(text_msg, images):
    """
    构造标准 OpenAI Chat Completions user content：
    - 无图片时返回纯文本字符串（100% 兼容所有文本模型）
    - 有图片时返回 [{"type": "text", ...}, {"type": "image_url", ...}] 多模态数组
    """
    clean_text = (text_msg or "").strip()
    valid_imgs = [im for im in (images or []) if isinstance(im, dict) and im.get("data_url")]
    if not valid_imgs:
        return clean_text

    if clean_text:
        prompt_text = (
            f"{clean_text}\n"
            f"（以上是好友同时发来的文字，下方附带好友发来的 {len(valid_imgs)} 张图片/表情包，请结合画面内容用自然日常口语一并回复）"
        )
    else:
        prompt_text = (
            f"（好友刚给你发来了 {len(valid_imgs)} 张图片/表情包，未附带文字。"
            f"请仔细看图，结合画面里的场景、人物、文字或表情包情绪，以主人本人的日常口语自然回复一句）"
        )

    parts = [{"type": "text", "text": prompt_text}]
    for im in valid_imgs:
        parts.append({
            "type": "image_url",
            "image_url": {"url": im["data_url"]}
        })
    return parts


# =============================================================================
# 2. 聊天区图片气泡视觉定位引擎 (纯 NumPy，无 OpenCV 依赖，自适应浅/深色主题)
# =============================================================================
def _find_connected_boxes(grid_mask, cell_size=4):
    """对下采样后的布尔栅格执行 4-连通分量提取，返回原坐标包围盒列表"""
    gh, gw = grid_mask.shape
    visited = np.zeros((gh, gw), dtype=bool)
    boxes = []
    for r in range(gh):
        for c in range(gw):
            if not grid_mask[r, c] or visited[r, c]:
                continue
            q = deque([(r, c)])
            visited[r, c] = True
            min_r = max_r = r
            min_c = max_c = c
            area = 0
            while q:
                cr, cc = q.popleft()
                area += 1
                if cr < min_r:
                    min_r = cr
                if cr > max_r:
                    max_r = cr
                if cc < min_c:
                    min_c = cc
                if cc > max_c:
                    max_c = cc
                for nr, nc in ((cr - 1, cc), (cr + 1, cc), (cr, cc - 1), (cr, cc + 1)):
                    if 0 <= nr < gh and 0 <= nc < gw and grid_mask[nr, nc] and not visited[nr, nc]:
                        visited[nr, nc] = True
                        q.append((nr, nc))
            # 过滤极小噪点
            if area >= 25:
                boxes.append((
                    min_c * cell_size,
                    min_r * cell_size,
                    (max_c + 1) * cell_size,
                    (max_r + 1) * cell_size,
                ))
    return boxes


def _get_last_outgoing_bottom_y(chat_bubbles, layout):
    """获取最后一条我方绿底气泡底部 Y 坐标；无我方气泡时返回主聊天区顶部"""
    header_y = int(layout["header_h"] + 16)
    if not chat_bubbles:
        return header_y
    for b in reversed(chat_bubbles):
        if not b.get("is_incoming", True):
            return max(header_y, int(b.get("last_y", header_y) + 22))
    return header_y


def detect_pending_image_bubbles(img_wechat, layout, ocr_res, chat_bubbles, max_images=3):
    """
    在最后一条我方绿底气泡下方，精准检测好友发来的未回复图片/大表情包缩略图。
    返回: [{"box": (x1,y1,x2,y2), "data_url": str, "img_hash": str, "w": int, "h": int}, ...]
    """
    if img_wechat is None:
        return []
    W, H = img_wechat.size
    chat_start_x = int(layout["chat_start_x"])
    input_h = int(layout["input_h"])
    chat_w = W - chat_start_x
    if chat_w < 200 or H < 200:
        return []

    y_top = _get_last_outgoing_bottom_y(chat_bubbles, layout)
    y_bot = int(H - input_h * 0.72)
    if y_bot - y_top < 65:
        return []

    # 水平范围严格避开左侧好友头像 (约 56~76px) 及右侧我方头像与滚动条
    avatar_margin = max(56, min(80, int(chat_w * 0.088)))
    x_left = min(W - 20, chat_start_x + avatar_margin)
    x_right = min(W - 24, chat_start_x + int(chat_w * 0.78))
    if x_right - x_left < 80:
        return []

    arr = np.array(img_wechat, dtype=np.uint8)
    roi = arr[y_top:y_bot, x_left:x_right]
    rh, rw, _ = roi.shape

    # 1. 动态采样聊天区主导背景色（兼容浅色/深色/纯色主题）
    sample = roi[::4, ::4].reshape(-1, 3)
    bg_rgb = np.median(sample, axis=0).astype(np.int16)

    roi_i16 = roi.astype(np.int16)
    diff = np.max(np.abs(roi_i16 - bg_rgb), axis=2)
    fg_mask = diff > 16

    # 2. 严格排除我方绿底气泡像素
    pr, pg, pb = roi_i16[:, :, 0], roi_i16[:, :, 1], roi_i16[:, :, 2]
    green_mask = (pg > pr + 18) & (pg > pb + 18) & (pg > 45)
    fg_mask[green_mask] = False

    # 3. 4x4 池化后连通域提取
    cell = 4
    gh, gw = rh // cell, rw // cell
    if gh < 12 or gw < 12:
        return []
    trimmed = fg_mask[:gh * cell, :gw * cell]
    grid = trimmed.reshape(gh, cell, gw, cell).mean(axis=(1, 3)) > 0.25
    raw_boxes = _find_connected_boxes(grid, cell_size=cell)
    if not raw_boxes:
        return []

    # 预处理 OCR 文字框以便区分“多行纯文字白底气泡”与“真实图片/截图/表情包”
    ocr_boxes = []
    for item in (ocr_res or []):
        try:
            b, txt, _ = item
            ox1 = min(pt[0] for pt in b)
            oy1 = min(pt[1] for pt in b)
            ox2 = max(pt[0] for pt in b)
            oy2 = max(pt[1] for pt in b)
            ocr_boxes.append((ox1, oy1, ox2, oy2, str(txt or "").strip()))
        except Exception:
            continue

    candidates = []
    for rx1, ry1, rx2, ry2 in raw_boxes:
        bw = rx2 - rx1
        bh = ry2 - ry1
        # 过滤小图标、单行文字气泡、语音条、时间条
        if bw < 65 or bh < 65:
            continue
        # 过滤异常横跨全屏的分割线或误抓整页
        if bw > int(chat_w * 0.72) and bh > int((y_bot - y_top) * 0.92):
            continue

        abs_x1 = x_left + rx1
        abs_y1 = y_top + ry1
        abs_x2 = min(W, x_left + rx2)
        abs_y2 = min(H, y_top + ry2)

        # 必须是左侧好友消息轨发出的气泡（起边靠左，排除靠右的其他元素）
        if abs_x1 > chat_start_x + int(chat_w * 0.34):
            continue

        patch = arr[abs_y1:abs_y2, abs_x1:abs_x2]
        if patch.size == 0:
            continue

        # 再次校验框内我方绿底占比，防止误框我方气泡
        p_i16 = patch.astype(np.int16)
        g_ratio = float(np.mean((p_i16[:, :, 1] > p_i16[:, :, 0] + 18) & (p_i16[:, :, 1] > p_i16[:, :, 2] + 18) & (p_i16[:, :, 1] > 45)))
        if g_ratio > 0.25:
            continue

        # 计算色彩丰富度与灰度层次
        chroma = np.max(patch, axis=2).astype(np.int16) - np.min(patch, axis=2).astype(np.int16)
        colorful_ratio = float(np.mean(chroma > 22))
        gray = 0.299 * patch[:, :, 0] + 0.587 * patch[:, :, 1] + 0.114 * patch[:, :, 2]
        gray_std = float(np.std(gray))

        # 统计落在该框内部的 OCR 文字
        inner_texts = []
        ocr_area = 0.0
        for ox1, oy1, ox2, oy2, otxt in ocr_boxes:
            ocx = (ox1 + ox2) * 0.5
            ocy = (oy1 + oy2) * 0.5
            if abs_x1 - 4 <= ocx <= abs_x2 + 4 and abs_y1 - 4 <= ocy <= abs_y2 + 4:
                inner_texts.append(otxt)
                ocr_area += max(0.0, (ox2 - ox1) * (oy2 - oy1))

        box_area = float(max(1, bw * bh))
        ocr_cover = ocr_area / box_area
        joined_inner = " ".join(inner_texts)

        # 排除转账/红包等系统卡片
        if any(h in joined_inner for h in SYSTEM_CARD_HINTS):
            continue

        # 检查是否为纯色白底/深灰底的多行纯文字聊天消息
        # 四角采样判定纯色底框
        corners = np.array([
            patch[min(3, bh - 1), min(3, bw - 1)],
            patch[min(3, bh - 1), max(0, bw - 4)],
            patch[max(0, bh - 4), min(3, bw - 1)],
            patch[max(0, bh - 4), max(0, bw - 4)],
        ], dtype=np.int16)
        corner_spread = float(np.max(np.ptp(corners, axis=0)))
        is_uniform_text_box = (
            len(inner_texts) >= 1
            and colorful_ratio < 0.035
            and corner_spread < 18
            and (ocr_cover > 0.10 or (len(inner_texts) >= 2 and gray_std < 55))
        )
        if is_uniform_text_box:
            continue

        # 最终准入：彩色图片/表情包/截图 或 清晰度丰富的黑白图片/表情包
        is_valid_image = (colorful_ratio >= 0.045) or (gray_std >= 26.0 and len(inner_texts) <= 1)
        if not is_valid_image:
            continue

        # 裁剪并微收 2px 抗锯齿边缘
        pad = 2 if (bw > 75 and bh > 75) else 0
        crop_img = img_wechat.crop((abs_x1 + pad, abs_y1 + pad, abs_x2 - pad, abs_y2 - pad))
        data_url, img_hash = encode_pil_to_data_url(crop_img)
        if data_url:
            candidates.append({
                "box": (abs_x1, abs_y1, abs_x2, abs_y2),
                "first_y": abs_y1,
                "last_y": abs_y2,
                "w": bw,
                "h": bh,
                "data_url": data_url,
                "img_hash": img_hash,
            })

    candidates.sort(key=lambda x: x["first_y"])
    return candidates[-max_images:]


def filter_text_bubbles_overlapping_images(pending_text_bubbles, image_bubbles):
    """过滤掉其实来自图片/截图内部的碎字 OCR 气泡，避免 AI 重复误读截图里的零碎文字"""
    if not pending_text_bubbles or not image_bubbles:
        return pending_text_bubbles
    kept = []
    for tb in pending_text_bubbles:
        ty = tb.get("first_y", 0)
        ty2 = tb.get("last_y", ty)
        mid_y = (ty + ty2) * 0.5
        inside_img = False
        for ib in image_bubbles:
            ix1, iy1, ix2, iy2 = ib["box"]
            if iy1 - 6 <= mid_y <= iy2 + 6:
                inside_img = True
                break
        if not inside_img:
            kept.append(tb)
    return kept


# =============================================================================
# 3. 语音气泡检测 + 右键安全触发微信「转文字」
# =============================================================================
def is_voice_duration_text(text):
    if not text:
        return False, 0
    m = VOICE_DURATION_RE.match(str(text).strip())
    if not m:
        return False, 0
    sec = int(m.group(1))
    if 1 <= sec <= 60:
        return True, sec
    return False, 0


def detect_pending_voice_bubbles(ocr_res, img_wechat, layout, chat_bubbles):
    """
    检测位于最后一条我方绿底气泡下方的左侧未转写语音气泡。
    若该语音气泡正下方已存在转写文字气泡，则视为已转写并跳过。
    """
    if not ocr_res or img_wechat is None:
        return []

    W, H = img_wechat.size
    arr = np.array(img_wechat, dtype=np.uint8)
    chat_start_x = int(layout["chat_start_x"])
    input_h = int(layout["input_h"])
    y_top = _get_last_outgoing_bottom_y(chat_bubbles, layout)
    y_bot = int(H - input_h * 0.70)

    # 先收集左侧所有非绿底文字中心，用于判断语音气泡下方是否已有转写文本
    left_other_ys = []
    voice_cands = []

    for b, txt, sc in ocr_res:
        c_clean = str(txt or "").strip()
        if not c_clean:
            continue
        cx = (b[0][0] + b[1][0]) * 0.5
        cy = (b[0][1] + b[2][1]) * 0.5
        box_left = min(pt[0] for pt in b)
        if not (cx > chat_start_x + 15 and y_top - 4 < cy < y_bot):
            continue

        # 采样是否我方绿底
        min_x = max(0, int(min(pt[0] for pt in b) - 12))
        max_x = min(W, int(max(pt[0] for pt in b) + 12))
        min_y = max(0, int(min(pt[1] for pt in b) - 6))
        max_y = min(H, int(max(pt[1] for pt in b) + 6))
        patch = arr[min_y:max_y, min_x:max_x]
        if patch.size == 0:
            continue
        pr, pg, pb = patch[:, :, 0].astype(int), patch[:, :, 1].astype(int), patch[:, :, 2].astype(int)
        has_green = int(np.sum((pg > pr + 18) & (pg > pb + 18) & (pg > 45))) > 15
        is_left = (not has_green) and (box_left < chat_start_x + int(0.55 * (W - chat_start_x)))
        if not is_left:
            continue

        is_voice, sec = is_voice_duration_text(c_clean)
        if is_voice:
            # 语音时长数字靠气泡右侧，往左偏移一点点更贴近语音气泡正中
            bubble_click_x = max(chat_start_x + 75, int(cx - 18))
            voice_cands.append({
                "cx": bubble_click_x,
                "cy": int(cy),
                "duration": sec,
                "raw": c_clean,
                "box": b,
            })
        else:
            # 排除时间条
            if not re.match(r'^\d{1,2}:\d{2}$', c_clean):
                left_other_ys.append((cy, c_clean))

    # 若语音气泡下方 18~95px 内已经紧跟了一段非语音文本（大概率是已展开的转写框），跳过
    pending = []
    for vc in voice_cands:
        already_transcribed = False
        for oy, otxt in left_other_ys:
            if 18 <= (oy - vc["cy"]) <= 95 and len(otxt) >= 2:
                already_transcribed = True
                break
        if not already_transcribed:
            pending.append(vc)

    pending.sort(key=lambda x: x["cy"])
    return pending


def _post_right_click(hwnd, client_x, client_y):
    lParam = (int(client_y) << 16) | (int(client_x) & 0xFFFF)
    user32.PostMessageW(hwnd, 0x0200, 0x0000, lParam)  # WM_MOUSEMOVE
    time.sleep(0.015)
    user32.PostMessageW(hwnd, 0x0204, 0x0002, lParam)  # WM_RBUTTONDOWN
    time.sleep(0.025)
    user32.PostMessageW(hwnd, 0x0205, 0x0000, lParam)  # WM_RBUTTONUP


def _post_left_click(hwnd, client_x, client_y):
    lParam = (int(client_y) << 16) | (int(client_x) & 0xFFFF)
    user32.PostMessageW(hwnd, 0x0200, 0x0000, lParam)
    time.sleep(0.015)
    user32.PostMessageW(hwnd, 0x0201, 0x0001, lParam)
    time.sleep(0.025)
    user32.PostMessageW(hwnd, 0x0202, 0x0000, lParam)


def _find_wechat_popup_menus(main_hwnd):
    """枚举与微信主窗口同 PID 的可见右键悬浮菜单窗口"""
    main_pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(main_hwnd, ctypes.byref(main_pid))
    if not main_pid.value:
        return []

    menus = []

    def enum_proc(h, lp):
        if h == main_hwnd or not user32.IsWindowVisible(h):
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value != main_pid.value:
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(h, ctypes.byref(rect)):
            return True
        w = rect.right - rect.left
        h_px = rect.bottom - rect.top
        # 微信右键菜单典型尺寸：宽 70~320px，高 50~560px
        if 70 <= w <= 340 and 50 <= h_px <= 560:
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, cls_buf, 256)
            menus.append((h, rect, w, h_px, cls_buf.value))
        return True

    cb = WNDENUMPROC(enum_proc)
    user32.EnumWindows(cb, 0)
    return menus


def _grab_small_window(hwnd, w, h, rect):
    """优先 PrintWindow 离屏抓取右键菜单，黑屏且在屏幕内时自动回退 ImageGrab"""
    hwnd_dc = mfc_dc = save_bmp = None
    img = None
    try:
        hwnd_dc = user32.GetWindowDC(hwnd)
        mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        save_bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        gdi32.SelectObject(mfc_dc, save_bmp)
        res = user32.PrintWindow(hwnd, mfc_dc, 2) or user32.PrintWindow(hwnd, mfc_dc, 0)
        if res:
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mfc_dc, save_bmp, 0, h, buf, ctypes.byref(bmi), 0)
            img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    except Exception:
        img = None
    finally:
        if save_bmp:
            gdi32.DeleteObject(save_bmp)
        if mfc_dc:
            gdi32.DeleteDC(mfc_dc)
        if hwnd_dc:
            user32.ReleaseDC(hwnd, hwnd_dc)

    if img is not None and np.array(img).max() > 10:
        return img
    if rect.left >= -50 and rect.top >= -50:
        try:
            return ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom)).convert("RGB")
        except Exception:
            pass
    return None


def trigger_voice_to_text_safe(hwnd, voice_bubble, ocr_engine, grab_fn, log_fn=None):
    """
    安全触发 PC 微信原生「转文字」：
    1. 右键点击语音气泡（0 鼠标物理移动）
    2. 枚举微信右键菜单弹窗并 OCR，严格匹配「转文字」后才点击其中心坐标（绝不盲点偏移！）
    3. 未找到「转文字」时立即发 ESC 关闭菜单并安全跳过
    4. 轮询等待下方转写文本渲染出现（最长 2.5s）
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    cx, cy = int(voice_bubble["cx"]), int(voice_bubble["cy"])
    dur = voice_bubble.get("duration", 0)
    _log(f"🎙️ [语音识别] 检测到好友 {dur}\" 未转写语音，正在安全触发微信「转文字」...")

    # 抓取点击前语音下方局部像素用于检测转写是否渲染完成
    img_before, _, _ = grab_fn(hwnd)
    before_hash = None
    if img_before is not None:
        W, H = img_before.size
        patch_b = np.array(img_before)[max(0, cy + 12):min(H, cy + 110), max(0, cx - 40):min(W, cx + 280)]
        before_hash = hashlib.md5(patch_b.tobytes()).hexdigest()

    # 1. 发送局部右键
    _post_right_click(hwnd, cx, cy)
    time.sleep(0.30)

    # 2. 查找微信右键弹出菜单并 OCR 精准寻找「转文字」
    menus = _find_wechat_popup_menus(hwnd)
    clicked_ok = False
    for m_hwnd, m_rect, mw, mh, m_cls in menus:
        m_img = _grab_small_window(m_hwnd, mw, mh, m_rect)
        if m_img is None:
            continue
        res_m, _ = ocr_engine(m_img)
        for b, txt, sc in (res_m or []):
            t = str(txt or "").strip()
            if "转文字" in t:
                mx = int((b[0][0] + b[1][0]) * 0.5)
                my = int((b[0][1] + b[2][1]) * 0.5)
                _post_left_click(m_hwnd, mx, my)
                clicked_ok = True
                _log(f"🎙️ [语音转文字] 已精准点击菜单「{t}」，等待微信转写渲染...")
                break
        if clicked_ok:
            break

    if not clicked_ok:
        # 安全熔断：发 ESC 关闭可能残留的右键菜单，绝不盲点！
        for m_hwnd, _, _, _, _ in menus:
            try:
                user32.PostMessageW(m_hwnd, 0x0100, 0x1B, 0)  # WM_KEYDOWN VK_ESCAPE
                user32.PostMessageW(m_hwnd, 0x0101, 0x1B, 0)  # WM_KEYUP VK_ESCAPE
            except Exception:
                pass
        try:
            user32.PostMessageW(hwnd, 0x0100, 0x1B, 0)
            user32.PostMessageW(hwnd, 0x0101, 0x1B, 0)
        except Exception:
            pass
        _log("⚠️ [语音转文字跳过] 未在右键菜单中识别到「转文字」（离屏模式可能受系统菜单限制），已安全取消。")
        return False

    # 3. 轮询等待微信转写框渲染完成（最长 2.5s）
    deadline = time.time() + 2.5
    while time.time() < deadline:
        time.sleep(0.35)
        img_now, _, _ = grab_fn(hwnd)
        if img_now is not None and before_hash is not None:
            W, H = img_now.size
            patch_n = np.array(img_now)[max(0, cy + 12):min(H, cy + 110), max(0, cx - 40):min(W, cx + 280)]
            if hashlib.md5(patch_n.tobytes()).hexdigest() != before_hash:
                time.sleep(0.25)  # 等待文字完全渲染沉降
                _log("✅ [语音转文字完成] 检测到转写文本已渲染，准备交由 OCR 与 AI 处理。")
                return True
    return True
