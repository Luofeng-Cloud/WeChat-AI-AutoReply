import os
import sys
import numpy as np
from PIL import Image, ImageDraw

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import media_vision as mv


def build_mock_wechat_canvas(dark=False):
    """构造 1000x700 仿真微信聊天窗口（浅色/深色主题）"""
    W, H = 1000, 700
    bg = (25, 25, 25) if dark else (245, 245, 245)
    bubble_white = (44, 44, 44) if dark else (255, 255, 255)
    text_color = (225, 225, 225) if dark else (30, 30, 30)
    green_bubble = (62, 181, 117) if dark else (149, 236, 105)

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    layout = {
        "sidebar_w": 60,
        "chat_start_x": 300,
        "header_h": 60,
        "input_h": 140,
        "total_h_px": H,
    }

    # 1. 上方历史区：好友发过的旧照片（在我方最后一条绿底气泡上方，不应再被检出！）
    draw.rectangle([380, 80, 520, 175], fill=(230, 90, 60))
    draw.ellipse([410, 95, 490, 160], fill=(40, 170, 230))

    # 2. 我方最后一条绿底回复气泡 (y=190..235)
    draw.rectangle([680, 190, 910, 235], fill=green_bubble)
    chat_bubbles = [
        {"is_incoming": False, "first_y": 212, "last_y": 212, "text": "好的收到"}
    ]

    # 3. 好友新消息区（在绿底气泡下方）：
    # 3a. 左侧好友头像 (x=312..352, y=260..300) —— 必须被排除
    draw.rectangle([312, 260, 352, 300], fill=(220, 120, 50))

    # 3b. 好友发的一段纯白/深灰多行纯文字气泡 (x=370..620, y=260..345) —— 必须被排除
    draw.rectangle([370, 260, 620, 345], fill=bubble_white)
    # 模拟内部两行纯色文字条纹
    draw.rectangle([386, 278, 595, 294], fill=text_color)
    draw.rectangle([386, 310, 550, 326], fill=text_color)

    # 3c. 好友发来一张彩色风景/美食照片 (x=370..550, y=370..500) —— 必须精准检出！
    draw.rectangle([370, 370, 550, 500], fill=(70, 150, 235))  # 蓝天
    draw.rectangle([370, 445, 550, 500], fill=(225, 165, 85)) # 桌面
    draw.ellipse([420, 395, 505, 470], fill=(230, 75, 65))    # 红色美食

    # 模拟 OCR 识别结果（包含纯文字气泡的两行文字，以及照片内部的 1 处误读杂字）
    ocr_res = [
        ([[695, 200], [895, 200], [895, 224], [695, 224]], "好的收到", 0.99),
        ([[385, 276], [595, 276], [595, 296], [385, 296]], "晚上一起吃饭吗", 0.98),
        ([[385, 308], [550, 308], [550, 328], [385, 328]], "看看我拍的菜", 0.97),
        ([[430, 420], [490, 420], [490, 440], [430, 440]], "招牌菜", 0.72),  # 图内碎字
    ]

    chat_bubbles.append({"is_incoming": True, "first_y": 286, "last_y": 318, "text": "晚上一起吃饭吗，看看我拍的菜"})
    chat_bubbles.append({"is_incoming": True, "first_y": 430, "last_y": 430, "text": "招牌菜"})
    return img, layout, ocr_res, chat_bubbles


def run_all_tests():
    # 1. 浅色主题图片检测 + 头像/纯文字/历史旧图过滤
    img_light, layout, ocr_res, bubbles = build_mock_wechat_canvas(dark=False)
    imgs_l = mv.detect_pending_image_bubbles(img_light, layout, ocr_res, bubbles)
    assert len(imgs_l) == 1, f"浅色主题应精准检出 1 张新图，实际={len(imgs_l)}: {[i['box'] for i in imgs_l]}"
    bx1, by1, bx2, by2 = imgs_l[0]["box"]
    assert 360 <= bx1 <= 385 and 360 <= by1 <= 385 and 535 <= bx2 <= 565 and 485 <= by2 <= 515, f"裁图坐标偏差: {imgs_l[0]['box']}"
    assert imgs_l[0]["data_url"].startswith("data:image/jpeg;base64,")
    print(f"[PASS] 1. 浅色微信画布精准检出好友新图 (box={imgs_l[0]['box']}, hash={imgs_l[0]['img_hash']})")

    # 2. 深色主题图片检测
    img_dark, layout_d, ocr_d, bubbles_d = build_mock_wechat_canvas(dark=True)
    imgs_d = mv.detect_pending_image_bubbles(img_dark, layout_d, ocr_d, bubbles_d)
    assert len(imgs_d) == 1, f"深色主题应精准检出 1 张新图，实际={len(imgs_d)}"
    print(f"[PASS] 2. 深色微信画布精准检出好友新图 (box={imgs_d[0]['box']})")

    # 3. 图内 OCR 碎字屏蔽（保留图外好友真实发言）
    pending_text = [b for b in bubbles if b["is_incoming"]]
    filtered_text = mv.filter_text_bubbles_overlapping_images(pending_text, imgs_l)
    assert len(filtered_text) == 1 and "晚上一起吃饭吗" in filtered_text[0]["text"], f"碎字过滤异常: {filtered_text}"
    print("[PASS] 3. 图内 OCR 碎字已屏蔽，图外好友文字完整保留")

    # 4. 多模态 Vision content 构造
    c_text = mv.build_multimodal_user_content("你好", [])
    assert c_text == "你好"
    c_img_only = mv.build_multimodal_user_content("", imgs_l)
    assert isinstance(c_img_only, list) and len(c_img_only) == 2 and c_img_only[1]["type"] == "image_url"
    c_mixed = mv.build_multimodal_user_content("这个好吃吗", imgs_l)
    assert isinstance(c_mixed, list) and "这个好吃吗" in c_mixed[0]["text"]
    print("[PASS] 4. OpenAI Vision 多模态请求结构（纯文/纯图/图文）校验通过")

    # 5. 语音时长正则 + 未转写/已转写语音气泡判定
    for good in ['3"', '12"', '5”', "8''", "60'"]:
        ok, sec = mv.is_voice_duration_text(good)
        assert ok and 1 <= sec <= 60, f"应识别为语音: {good}"
    for bad in ["666", "12:30", '99"', '0"', "5元", "你好"]:
        ok, _ = mv.is_voice_duration_text(bad)
        assert not ok, f"不应误判为语音: {bad}"

    # 构造两条语音：y=280 已在 y=325 展开转写文字（应跳过）；y=420 下方空白（应检出）
    voice_ocr = [
        ([[410, 270], [450, 270], [450, 290], [410, 290]], '6"', 0.96),
        ([[380, 315], [560, 315], [560, 338], [380, 338]], "明天下午三点开会", 0.98),
        ([[410, 410], [455, 410], [455, 430], [410, 430]], '14"', 0.95),
    ]
    plain_img = Image.new("RGB", (1000, 700), (245, 245, 245))
    voices = mv.detect_pending_voice_bubbles(voice_ocr, plain_img, layout, [{"is_incoming": False, "last_y": 200}])
    assert len(voices) == 1 and voices[0]["duration"] == 14, f"未转写语音检测异常: {voices}"
    print(f"[PASS] 5. 语音时长识别与已转写去重校验通过 (精准检出 14\" 未转写语音)")

    print("\n✅ 全部多模态识图与语音自动化测试 PASS，零缺陷！")


if __name__ == "__main__":
    run_all_tests()
