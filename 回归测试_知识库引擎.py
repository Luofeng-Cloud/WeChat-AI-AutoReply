import os
import sys
import time
import zipfile
import tempfile
import shutil

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import knowledge_base as kb


def make_sample_docx(path):
    """动态构造一个标准 .docx（含段落与表格）用于离线测试解析器"""
    doc_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>门店营业时间与售后政策</w:t></w:r></w:p>
    <w:p><w:r><w:t>工作日营业时间为上午9点到晚上9点，周末照常营业。</w:t></w:r></w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>商品型号</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>售价</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>质保期</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>ProMax-2026</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>1299元</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>两年包换</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", doc_xml.encode("utf-8"))


def run_all_tests():
    tmp_root = tempfile.mkdtemp(prefix="kb_test_")
    try:
        kb_dir = os.path.join(tmp_root, "kb_store")
        src_dir = os.path.join(tmp_root, "src")
        os.makedirs(src_dir, exist_ok=True)

        mgr_gui = kb.KnowledgeBaseManager(kb_dir=kb_dir)
        mgr_bot = kb.KnowledgeBaseManager(kb_dir=kb_dir)

        # 1. 空库检索应返回 empty
        res_empty = mgr_bot.retrieve("你好")
        assert res_empty["mode"] == "empty" and res_empty["context"] == "", f"空库异常: {res_empty}"
        print("[PASS] 1. 空知识库安全返回 empty")

        # 2. GBK 编码 TXT + UTF-8 CSV + DOCX 解析
        txt_path = os.path.join(src_dir, "个人日程.txt")
        with open(txt_path, "wb") as f:
            f.write("周三下午两点要开产品评审会，晚上七点和老王打羽毛球。".encode("gbk"))

        csv_path = os.path.join(src_dir, "价格表.csv")
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("套餐名称,价格,包含内容\n基础版,99元,每月10次咨询\n旗舰版,299元,无限次咨询+专属客服\n")

        docx_path = os.path.join(src_dir, "售后说明.docx")
        make_sample_docx(docx_path)

        d1 = mgr_gui.add_file(txt_path)
        d2 = mgr_gui.add_file(csv_path)
        d3 = mgr_gui.add_file(docx_path)
        assert d1["char_count"] > 10 and d2["char_count"] > 10 and d3["char_count"] > 20
        print(f"[PASS] 2. 多格式导入解析成功 (TXT={d1['char_count']}字, CSV={d2['char_count']}字, DOCX={d3['char_count']}字)")

        # 3. 跨实例热更新 + 小文档直通模式
        res_direct = mgr_bot.retrieve("旗舰版多少钱？")
        assert res_direct["mode"] == "direct", f"应走直通模式: {res_direct}"
        assert "299元" in res_direct["context"] and "ProMax-2026" in res_direct["context"] and "羽毛球" in res_direct["context"]
        prompt_block = kb.build_kb_prompt_block(res_direct)
        assert "严禁说出" in prompt_block and "299元" in prompt_block
        print(f"[PASS] 3. 跨进程热加载 + 小文档直通模式 + 去客服腔 Prompt 构造 ({res_direct['elapsed_ms']}ms)")

        # 4. 新建大文档笔记，触发 BM25 检索模式
        big_sections = [
            f"第{i}章 通用背景资料：关于日常行政流程、考勤打卡、报销发票与会议室预约的常规补充说明内容，编号SEC-{i}。" * 6
            for i in range(1, 22)
        ]
        big_sections.insert(
            10,
            "【特价机票与退款规则】如果行程取消，提前48小时申请可全额免手续费退票；起飞前24小时内退票收取15%手续费，联系值班热线400-888-9999。"
        )
        big_sections.insert(
            16,
            "【Wi-Fi 密码与访客网络】公司五楼访客 WiFi 账号是 Guest-5F，连接密码为 Happy2026!，单次授权有效期为12小时。"
        )
        d4 = mgr_gui.add_text_note("公司综合手册", "\n\n".join(big_sections))
        assert d4["char_count"] > 3000 and d4["chunk_count"] >= 8

        # 4a. 精准命中 WiFi 密码
        res_wifi = mgr_bot.retrieve("五楼访客WiFi密码是什么？", {"direct_threshold": 3000, "top_k": 3})
        assert res_wifi["mode"] == "bm25", f"应切换 BM25: {res_wifi}"
        assert "Happy2026!" in res_wifi["context"], f"未命中 WiFi 密码: {res_wifi}"
        print(f"[PASS] 4a. 大文档 BM25 精准召回 Wi-Fi 密码 (Top1 分值={res_wifi['hits'][0]['score']}, {res_wifi['elapsed_ms']}ms)")

        # 4b. 精准命中退票规则
        res_refund = mgr_bot.retrieve("提前两天取消机票退票收手续费吗", {"direct_threshold": 3000, "top_k": 3})
        assert res_refund["mode"] == "bm25" and "400-888-9999" in res_refund["context"], f"未命中退票规则: {res_refund}"
        print(f"[PASS] 4b. 大文档 BM25 精准召回退票规则 (Top1 分值={res_refund['hits'][0]['score']}, {res_refund['elapsed_ms']}ms)")

        # 4c. 无关闲聊不误注入噪音
        res_chat = mgr_bot.retrieve("哈哈哈哈在吗吃了吗", {"direct_threshold": 3000, "top_k": 3})
        assert res_chat["mode"] == "no_match" and res_chat["context"] == "", f"无关闲聊不应注入知识库: {res_chat}"
        print(f"[PASS] 4c. 无关闲聊自动过滤不注入噪音 ({res_chat['elapsed_ms']}ms)")

        # 5. 停用文档与删除文档
        mgr_gui.toggle_document(d4["id"], enabled=False)
        res_after_disable = mgr_bot.retrieve("WiFi密码")
        assert res_after_disable["mode"] == "direct" and "Happy2026!" not in res_after_disable["context"]
        ok_del = mgr_gui.delete_document(d4["id"])
        assert ok_del is True and len(mgr_bot.list_documents()) == 3
        print("[PASS] 5. 文档停用/删除 + 热更新回切直通模式正常")

        # 6. 损坏 DOCX / 不支持格式 / 总开关关闭异常兜底
        bad_docx = os.path.join(src_dir, "broken.docx")
        with open(bad_docx, "w", encoding="utf-8") as f:
            f.write("not a zip")
        try:
            mgr_gui.add_file(bad_docx)
            raise AssertionError("损坏 docx 应该报错")
        except ValueError:
            pass

        res_disabled = mgr_bot.retrieve("旗舰版多少钱", {"enabled": False})
        assert res_disabled["mode"] == "disabled" and res_disabled["context"] == ""
        print("[PASS] 6. 异常文件防护与总开关关闭校验通过")

        # 7. 若已安装 pypdf，实测 PDF 解析链路
        if kb.is_pdf_supported():
            from pypdf import PdfWriter
            pdf_path = os.path.join(src_dir, "guide.pdf")
            # 构造一份包含可提取 ASCII 文本的合法单页 PDF
            minimal_pdf = (
                b"%PDF-1.4\n"
                b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
                b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
                b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
                b"4 0 obj << /Length 68 >> stream\nBT /F1 12 Tf 10 100 Td (VIP Support Hotline 400-123-4567 Open 24h) Tj ET\nendstream endobj\n"
                b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
                b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000241 00000 n \n0000000360 00000 n \n"
                b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n430\n%%EOF\n"
            )
            with open(pdf_path, "wb") as f:
                f.write(minimal_pdf)
            d_pdf = mgr_gui.add_file(pdf_path)
            assert "400-123-4567" in mgr_bot.retrieve("hotline")["context"]
            print(f"[PASS] 7. PDF 解析与检索校验通过 ({d_pdf['char_count']} 字符)")
        else:
            print("[SKIP] 7. 当前环境未安装 pypdf，跳过 PDF 实测")

        print("\n✅ 全部知识库自动化测试 PASS，零缺陷！")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    run_all_tests()
