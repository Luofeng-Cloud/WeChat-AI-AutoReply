import os
import sys
import re
import csv
import io
import json
import math
import time
import uuid
import shutil
import zipfile
import threading
import xml.etree.ElementTree as ET
from collections import Counter

# =============================================================================
# 路径与基础常量 (兼容源码运行与 PyInstaller 打包模式)
# =============================================================================
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

KB_DIR = os.path.join(BASE_DIR, "knowledge_base")
KB_FILES_DIR = os.path.join(KB_DIR, "files")
KB_INDEX_FILE = os.path.join(KB_DIR, "kb_index.json")

SUPPORTED_EXTS = (".txt", ".md", ".csv", ".docx", ".pdf")

DEFAULT_KB_CONFIG = {
    "enabled": True,
    "direct_threshold": 3000,   # 已启用总字数 <= 此值时，直接全量注入上下文（准确率最高）
    "top_k": 3,                 # 大文档模式下最多召回的片段数
    "max_context_chars": 1800,  # 单次注入 Prompt 的参考资料最大字符数
    "chunk_size": 360,          # 切片目标字符数
    "chunk_overlap": 60,        # 相邻切片重叠字符数
    "min_score": 0.35           # BM25 最低相关性阈值（低于此值视为无关，防噪音干扰闲聊）
}

# 常见中文/英文高频停用词（过滤后显著提升 BM25 精准度）
STOPWORDS = set(
    "的了呢啊吧吗哦嗯嘛呀哈在是我你他她它这那和与及就都也还很有个一不人"
    "会要可以么什么怎么如何为什么请问一下帮我想问知道有没有能能不能"
    "the a an and or is are was were be to of in for on at with by from as"
    .split()
) | set(list("的了呢啊吧吗哦嗯嘛呀哈在是我你他她它这那和与及就都也还很有个一不"))

_LOCK = threading.RLock()


# =============================================================================
# 1. 文档解析层 (零重依赖：TXT / MD / CSV / DOCX 原生解析 + PDF 轻量 pypdf)
# =============================================================================
def _decode_bytes_smart(raw_bytes):
    """智能多编码解码文本字节，兼容 UTF-8 / UTF-8-BOM / GB18030 / GBK / UTF-16"""
    if not raw_bytes:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16"):
        try:
            return raw_bytes.decode(enc)
        except Exception:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _parse_txt_or_md(file_path):
    with open(file_path, "rb") as f:
        return _decode_bytes_smart(f.read())


def _parse_csv(file_path):
    with open(file_path, "rb") as f:
        text = _decode_bytes_smart(f.read())
    rows = []
    try:
        reader = csv.reader(io.StringIO(text))
        all_rows = [[cell.strip() for cell in r] for r in reader if any(c.strip() for c in r)]
    except Exception:
        return text

    if not all_rows:
        return ""

    # 若首行包含有效表头且总行数 >= 2，转为 "表头: 值" 易读格式，利于大模型理解表格
    header = all_rows[0]
    use_header = len(all_rows) >= 2 and len(header) >= 2 and all(len(h) <= 30 for h in header)
    if use_header:
        for r in all_rows[1:]:
            pairs = []
            for idx, val in enumerate(r):
                if not val:
                    continue
                col_name = header[idx] if idx < len(header) and header[idx] else f"列{idx + 1}"
                pairs.append(f"{col_name}: {val}")
            if pairs:
                rows.append(" | ".join(pairs))
    else:
        for r in all_rows:
            line = " | ".join(c for c in r if c)
            if line:
                rows.append(line)
    return "\n".join(rows)


def _parse_docx(file_path):
    """纯标准库解析 Word .docx（按原文顺序提取正文段落与表格内容，零外部依赖）"""
    if not zipfile.is_zipfile(file_path):
        raise ValueError("该 .docx 文件已损坏、受密码保护或并非标准 Word 2007+ (.docx) 格式")

    W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def _extract_p_text(p_elem):
        parts = []
        for node in p_elem.iter():
            tag = node.tag
            if tag == f"{W_NS}t" and node.text:
                parts.append(node.text)
            elif tag == f"{W_NS}tab":
                parts.append("\t")
            elif tag in (f"{W_NS}br", f"{W_NS}cr"):
                parts.append("\n")
        return "".join(parts).strip()

    lines = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "word/document.xml" not in zf.namelist():
                raise ValueError("docx 结构缺少 word/document.xml，无法提取文本")
            xml_bytes = zf.read("word/document.xml")
            root = ET.fromstring(xml_bytes)
            body = root.find(f"{W_NS}body")
            if body is None:
                return ""
            for child in body:
                if child.tag == f"{W_NS}p":
                    t = _extract_p_text(child)
                    if t:
                        lines.append(t)
                elif child.tag == f"{W_NS}tbl":
                    for tr in child.findall(f".//{W_NS}tr"):
                        cells = []
                        for tc in tr.findall(f"{W_NS}tc"):
                            cell_ps = [_extract_p_text(p) for p in tc.findall(f".//{W_NS}p")]
                            cell_text = " ".join(x for x in cell_ps if x).strip()
                            if cell_text:
                                cells.append(cell_text)
                        if cells:
                            lines.append(" | ".join(cells))
    except zipfile.BadZipFile:
        raise ValueError("docx 压缩包损坏，无法读取")
    return "\n".join(lines)


def is_pdf_supported():
    try:
        import pypdf  # noqa: F401
        return True
    except Exception:
        try:
            import PyPDF2  # noqa: F401
            return True
        except Exception:
            return False


def _parse_pdf(file_path):
    """使用轻量级纯 Python 库 pypdf（或回退 PyPDF2）提取文字版 PDF"""
    PdfReader = None
    try:
        from pypdf import PdfReader as _Reader
        PdfReader = _Reader
    except Exception:
        try:
            from PyPDF2 import PdfReader as _Reader
            PdfReader = _Reader
        except Exception:
            raise RuntimeError(
                "解析 PDF 需要安装轻量库 pypdf（命令: pip install pypdf），"
                "或者您可以先将 PDF 转为 TXT / Word(.docx) 后再导入。"
            )

    try:
        reader = PdfReader(file_path)
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                raise ValueError("该 PDF 已加密，无法直接提取文字，请先解除密码后再导入")
        pages_text = []
        for page in reader.pages:
            t = (page.extract_text() or "").strip()
            if t:
                pages_text.append(t)
        full_text = "\n\n".join(pages_text)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"PDF 解析失败: {e}")

    cleaned_alnum = re.sub(r"\s+", "", full_text)
    if len(cleaned_alnum) < 10:
        raise ValueError(
            "未从该 PDF 提取到有效文字（可能是纯图片扫描件），请先转为可复制文字的版本或 TXT/Word 后再导入。"
        )
    return full_text


def extract_text_from_file(file_path):
    """统一文件提取入口，返回规范化后的纯文本"""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"找不到文件: {file_path}")
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"暂不支持的文件格式: {ext}（目前支持: {', '.join(SUPPORTED_EXTS)}）")

    if ext in (".txt", ".md"):
        raw = _parse_txt_or_md(file_path)
    elif ext == ".csv":
        raw = _parse_csv(file_path)
    elif ext == ".docx":
        raw = _parse_docx(file_path)
    elif ext == ".pdf":
        raw = _parse_pdf(file_path)
    else:
        raw = ""

    return normalize_text(raw)


def normalize_text(text):
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# =============================================================================
# 2. 智能段落切片引擎 (优先按自然段/句子边界切分，带上下文重叠)
# =============================================================================
def split_text_to_chunks(text, chunk_size=360, overlap=60):
    text = normalize_text(text)
    if not text:
        return []
    chunk_size = max(120, int(chunk_size))
    overlap = max(0, min(int(overlap), chunk_size // 3))

    if len(text) <= chunk_size:
        return [{"idx": 0, "text": text, "char_count": len(text)}]

    # 按段落切分，保留大块结构
    raw_paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    # 对超长段落进一步按行或中文/英文句末标点细分
    units = []
    for p in raw_paras:
        if len(p) <= chunk_size:
            units.append(p)
        else:
            # 先按单换行拆
            sub_lines = [ln.strip() for ln in p.split("\n") if ln.strip()]
            for ln in sub_lines:
                if len(ln) <= chunk_size:
                    units.append(ln)
                else:
                    # 按句末标点切分，保留标点
                    sents = re.split(r"(?<=[。！？；.!?;])", ln)
                    cur = ""
                    for s in sents:
                        if not s:
                            continue
                        if len(cur) + len(s) <= chunk_size:
                            cur += s
                        else:
                            if cur:
                                units.append(cur.strip())
                            # 若单句依然超长，硬切
                            while len(s) > chunk_size:
                                units.append(s[:chunk_size].strip())
                                s = s[chunk_size - overlap:]
                            cur = s
                    if cur.strip():
                        units.append(cur.strip())

    # 将细粒度单元聚合为目标长度的切片，并添加重叠尾部防跨段断义
    chunks = []
    buf = ""
    for u in units:
        sep = "\n" if buf else ""
        if len(buf) + len(sep) + len(u) <= chunk_size:
            buf = f"{buf}{sep}{u}"
        else:
            if buf:
                chunks.append(buf.strip())
                tail = buf[-overlap:].strip() if overlap > 0 else ""
                if tail and len(tail) + 1 + len(u) <= chunk_size:
                    buf = f"{tail}\n{u}"
                else:
                    buf = u
            else:
                buf = u
    if buf.strip():
        chunks.append(buf.strip())

    return [{"idx": i, "text": c, "char_count": len(c)} for i, c in enumerate(chunks)]


# =============================================================================
# 3. 轻量中英文分词 + 本地 BM25 检索引擎 (纯 Python，0 外部依赖)
# =============================================================================
_RE_TOKEN_SPAN = re.compile(r"[a-z0-9_.\-]+|[\u4e00-\u9fff]+")


def tokenize_mixed(text):
    """
    中英文混合轻量分词：
    - 英文/数字：保留完整词（如 iphone16, 299, vip）
    - 中文连续段：提取全部双字词 (bigram) + 过滤停用词后的单字 (unigram)
      配合 BM25 的 IDF 权重，对专有名词、短语、数字型号具有极佳召回精度。
    """
    if not text:
        return []
    text = text.lower()
    tokens = []
    for span in _RE_TOKEN_SPAN.findall(text):
        if span[0].isascii():
            if len(span) >= 2 or span.isdigit():
                if span not in STOPWORDS:
                    tokens.append(span)
        else:
            n = len(span)
            # 双字词 bigram（对词组精确匹配贡献最大）
            for i in range(n - 1):
                bg = span[i:i + 2]
                if not (bg[0] in STOPWORDS and bg[1] in STOPWORDS):
                    tokens.append(bg)
            # 去停用词单字 unigram（保证短问句/单字关键词不漏召回）
            for ch in span:
                if ch not in STOPWORDS:
                    tokens.append(ch)
    return tokens


class KnowledgeBaseManager:
    def __init__(self, kb_dir=None):
        self.kb_dir = kb_dir or KB_DIR
        self.files_dir = os.path.join(self.kb_dir, "files")
        self.index_file = os.path.join(self.kb_dir, "kb_index.json")

        self._cached_sig = None
        self._data = {"version": 1, "updated_at": 0.0, "documents": []}
        # 小文档直通模式内存文本缓存（索引变化时自动清空，避免重复读盘解析）
        self._doc_text_cache = {}
        # BM25 内存倒排缓存
        self._bm25_ready = False
        self._bm25_chunks = []       # [{"doc_id", "doc_name", "idx", "text", "tf", "len"}]
        self._bm25_df = Counter()
        self._bm25_avgdl = 1.0

    def _ensure_dirs(self):
        os.makedirs(self.files_dir, exist_ok=True)

    def _file_sig(self):
        try:
            st = os.stat(self.index_file)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load_if_needed(self):
        with _LOCK:
            sig = self._file_sig()
            if sig == self._cached_sig:
                return
            if sig is None:
                self._data = {"version": 1, "updated_at": 0.0, "documents": []}
                self._cached_sig = None
                self._doc_text_cache.clear()
                self._bm25_ready = False
                return
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("documents"), list):
                    self._data = loaded
                else:
                    self._data = {"version": 1, "updated_at": 0.0, "documents": []}
                self._cached_sig = sig
                self._doc_text_cache.clear()
                self._bm25_ready = False
            except Exception:
                # 读失败时保持上一份有效内存缓存，绝不崩溃
                pass

    def _save_atomic(self):
        with _LOCK:
            self._ensure_dirs()
            self._data["updated_at"] = time.time()
            tmp_path = f"{self.index_file}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.index_file)
            self._cached_sig = self._file_sig()
            self._doc_text_cache.clear()
            self._bm25_ready = False

    def _rebuild_bm25_if_needed(self):
        if self._bm25_ready:
            return
        chunks = []
        df = Counter()
        total_len = 0
        for doc in self._data.get("documents", []):
            if not doc.get("enabled", True):
                continue
            d_id = doc.get("id", "")
            d_name = doc.get("name", "未命名文档")
            for ch in doc.get("chunks", []):
                txt = (ch.get("text") or "").strip()
                if not txt:
                    continue
                # 将文档标题也纳入索引词（让“报价单怎么写”能更好命中《报价单.docx》）
                toks = tokenize_mixed(f"{d_name}\n{txt}")
                tf = Counter(toks)
                c_len = len(toks)
                total_len += c_len
                for t in tf.keys():
                    df[t] += 1
                chunks.append({
                    "doc_id": d_id,
                    "doc_name": d_name,
                    "idx": ch.get("idx", 0),
                    "text": txt,
                    "tf": tf,
                    "len": c_len,
                })
        self._bm25_chunks = chunks
        self._bm25_df = df
        self._bm25_avgdl = (total_len / len(chunks)) if chunks else 1.0
        self._bm25_ready = True

    # -------------------------------------------------------------------------
    # CRUD 文档管理接口 (供 GUI 调用)
    # -------------------------------------------------------------------------
    def list_documents(self):
        self._load_if_needed()
        with _LOCK:
            res = []
            for d in self._data.get("documents", []):
                res.append({
                    "id": d.get("id", ""),
                    "name": d.get("name", ""),
                    "ext": d.get("ext", ""),
                    "enabled": bool(d.get("enabled", True)),
                    "char_count": int(d.get("char_count", 0)),
                    "chunk_count": len(d.get("chunks", [])),
                    "added_at": d.get("added_at", ""),
                })
            return res

    def get_document(self, doc_id):
        self._load_if_needed()
        with _LOCK:
            for d in self._data.get("documents", []):
                if d.get("id") == doc_id:
                    return dict(d)
            return None

    def get_stats(self, direct_threshold=None):
        self._load_if_needed()
        thr = int(direct_threshold or DEFAULT_KB_CONFIG["direct_threshold"])
        with _LOCK:
            docs = self._data.get("documents", [])
            enabled_docs = [d for d in docs if d.get("enabled", True)]
            enabled_chars = sum(int(d.get("char_count", 0)) for d in enabled_docs)
            total_chunks = sum(len(d.get("chunks", [])) for d in enabled_docs)
            if enabled_chars == 0:
                mode = "empty"
            elif enabled_chars <= thr:
                mode = "direct"
            else:
                mode = "bm25"
            return {
                "total_docs": len(docs),
                "enabled_docs": len(enabled_docs),
                "enabled_chars": enabled_chars,
                "enabled_chunks": total_chunks,
                "mode": mode,
                "direct_threshold": thr,
            }

    def add_file(self, src_path, chunk_size=None, chunk_overlap=None):
        """导入用户选择的本地文件，解析、切片并持久化"""
        self._load_if_needed()
        text = extract_text_from_file(src_path)
        if not text:
            raise ValueError("文件内容为空，未提取到任何有效文字。")

        orig_name = os.path.basename(src_path)
        ext = os.path.splitext(orig_name)[1].lower()
        doc_id = f"kb_{int(time.time())}_{uuid.uuid4().hex[:6]}"

        cs = int(chunk_size or DEFAULT_KB_CONFIG["chunk_size"])
        co = int(chunk_overlap or DEFAULT_KB_CONFIG["chunk_overlap"])
        chunks = split_text_to_chunks(text, chunk_size=cs, overlap=co)

        with _LOCK:
            self._ensure_dirs()
            stored_rel = os.path.join("files", f"{doc_id}{ext}")
            stored_abs = os.path.join(self.kb_dir, stored_rel)
            shutil.copy2(src_path, stored_abs)

            doc_obj = {
                "id": doc_id,
                "name": orig_name,
                "ext": ext,
                "stored_path": stored_rel,
                "enabled": True,
                "char_count": len(text),
                "chunk_count": len(chunks),
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "chunks": chunks,
            }
            self._data.setdefault("documents", []).append(doc_obj)
            self._save_atomic()
            return doc_obj

    def add_text_note(self, title, content, chunk_size=None, chunk_overlap=None):
        """直接在控制中心新建纯文本知识笔记并入库"""
        self._load_if_needed()
        title = (title or "").strip() or f"自定义笔记_{time.strftime('%m%d_%H%M')}"
        # 清理文件名非法字符
        safe_title = re.sub(r'[\\/:*?"<>|]+', "_", title)
        if not safe_title.lower().endswith((".txt", ".md")):
            safe_title += ".txt"

        text = normalize_text(content)
        if not text:
            raise ValueError("笔记内容不能为空。")

        doc_id = f"kb_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        cs = int(chunk_size or DEFAULT_KB_CONFIG["chunk_size"])
        co = int(chunk_overlap or DEFAULT_KB_CONFIG["chunk_overlap"])
        chunks = split_text_to_chunks(text, chunk_size=cs, overlap=co)

        with _LOCK:
            self._ensure_dirs()
            stored_rel = os.path.join("files", f"{doc_id}.txt")
            stored_abs = os.path.join(self.kb_dir, stored_rel)
            with open(stored_abs, "w", encoding="utf-8") as f:
                f.write(text)

            doc_obj = {
                "id": doc_id,
                "name": safe_title,
                "ext": ".txt",
                "stored_path": stored_rel,
                "enabled": True,
                "char_count": len(text),
                "chunk_count": len(chunks),
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "chunks": chunks,
            }
            self._data.setdefault("documents", []).append(doc_obj)
            self._save_atomic()
            return doc_obj

    def toggle_document(self, doc_id, enabled=None):
        self._load_if_needed()
        with _LOCK:
            for d in self._data.get("documents", []):
                if d.get("id") == doc_id:
                    new_state = (not bool(d.get("enabled", True))) if enabled is None else bool(enabled)
                    d["enabled"] = new_state
                    self._save_atomic()
                    return new_state
            return None

    def delete_document(self, doc_id):
        self._load_if_needed()
        with _LOCK:
            docs = self._data.get("documents", [])
            target = None
            remain = []
            for d in docs:
                if d.get("id") == doc_id:
                    target = d
                else:
                    remain.append(d)
            if target is None:
                return False
            self._data["documents"] = remain
            # 清理持久化副本文件
            rel = target.get("stored_path")
            if rel:
                abs_p = os.path.join(self.kb_dir, rel)
                try:
                    if os.path.exists(abs_p):
                        os.remove(abs_p)
                except Exception:
                    pass
            self._save_atomic()
            return True

    # -------------------------------------------------------------------------
    # 双模式检索核心 (小文档直通 + 大文档 BM25)
    # -------------------------------------------------------------------------
    def retrieve(self, query, cfg=None):
        t0 = time.perf_counter()
        self._load_if_needed()

        merged = dict(DEFAULT_KB_CONFIG)
        if isinstance(cfg, dict):
            for k in merged:
                if k in cfg and cfg[k] is not None:
                    merged[k] = cfg[k]

        if not merged.get("enabled", True):
            return {
                "mode": "disabled",
                "context": "",
                "hits": [],
                "total_enabled_docs": 0,
                "total_enabled_chars": 0,
                "elapsed_ms": 0.0,
            }

        try:
            direct_threshold = max(200, int(merged["direct_threshold"]))
        except Exception:
            direct_threshold = DEFAULT_KB_CONFIG["direct_threshold"]
        try:
            top_k = max(1, min(10, int(merged["top_k"])))
        except Exception:
            top_k = DEFAULT_KB_CONFIG["top_k"]
        try:
            max_chars = max(300, int(merged["max_context_chars"]))
        except Exception:
            max_chars = DEFAULT_KB_CONFIG["max_context_chars"]
        try:
            min_score = float(merged["min_score"])
        except Exception:
            min_score = DEFAULT_KB_CONFIG["min_score"]

        with _LOCK:
            enabled_docs = [d for d in self._data.get("documents", []) if d.get("enabled", True)]
            if not enabled_docs:
                return {
                    "mode": "empty",
                    "context": "",
                    "hits": [],
                    "total_enabled_docs": 0,
                    "total_enabled_chars": 0,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                }

            total_chars = sum(int(d.get("char_count", 0)) for d in enabled_docs)

            # 模式 1：小文档直通全量挂载（不漏任何细节，最适合个人简介/FAQ/报价单）
            if total_chars <= direct_threshold:
                blocks = []
                hits = []
                for d in enabled_docs:
                    d_name = d.get("name", "知识库")
                    full_txt = self._reconstruct_doc_text(d)
                    if full_txt:
                        blocks.append(f"【文档：{d_name}】\n{full_txt}")
                        hits.append({
                            "doc_name": d_name,
                            "chunk_idx": -1,
                            "score": 100.0,
                            "text": full_txt[:200] + ("..." if len(full_txt) > 200 else ""),
                        })
                ctx = "\n\n".join(blocks)
                # 直通模式下上限按 max(direct_threshold, max_chars) 控制，确保小文档完整注入
                hard_cap = max(direct_threshold + 400, max_chars)
                if len(ctx) > hard_cap:
                    ctx = ctx[:hard_cap] + "..."
                return {
                    "mode": "direct",
                    "context": ctx,
                    "hits": hits,
                    "total_enabled_docs": len(enabled_docs),
                    "total_enabled_chars": total_chars,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                }

            # 模式 2：大文档本地 BM25 + 混合子串加权检索
            q_str = (query or "").strip()
            q_tokens = tokenize_mixed(q_str)
            if not q_tokens:
                return {
                    "mode": "no_match",
                    "context": "",
                    "hits": [],
                    "total_enabled_docs": len(enabled_docs),
                    "total_enabled_chars": total_chars,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                }

            self._rebuild_bm25_if_needed()
            N = len(self._bm25_chunks)
            if N == 0:
                return {
                    "mode": "empty",
                    "context": "",
                    "hits": [],
                    "total_enabled_docs": len(enabled_docs),
                    "total_enabled_chars": total_chars,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                }

            k1 = 1.5
            b = 0.75
            avgdl = self._bm25_avgdl or 1.0
            q_unique = list(dict.fromkeys(q_tokens))
            # 抽取长度 >= 2 的有效词作为精确子串加分项
            exact_terms = [t for t in q_unique if len(t) >= 2]

            scored = []
            for ch in self._bm25_chunks:
                tf = ch["tf"]
                dl = ch["len"]
                score = 0.0
                matched_terms = 0
                for term in q_unique:
                    f = tf.get(term, 0)
                    if f <= 0:
                        continue
                    matched_terms += 1
                    df_t = self._bm25_df.get(term, 0)
                    idf = math.log((N - df_t + 0.5) / (df_t + 0.5) + 1.0)
                    # 双字词或英文词比单字词更具区分度，适当提升权重
                    weight = 1.35 if len(term) >= 2 else 0.65
                    denom = f + k1 * (1.0 - b + b * (dl / avgdl))
                    score += weight * idf * (f * (k1 + 1.0)) / denom

                if score <= 0:
                    continue

                # 精确词组原样出现加成（提升商品型号/专有名词排序）
                lower_txt = ch["text"].lower()
                for et in exact_terms:
                    if et in lower_txt:
                        score += 0.35

                if score >= min_score and matched_terms >= 1:
                    scored.append((score, ch))

            if not scored:
                return {
                    "mode": "no_match",
                    "context": "",
                    "hits": [],
                    "total_enabled_docs": len(enabled_docs),
                    "total_enabled_chars": total_chars,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                }

            scored.sort(key=lambda x: x[0], reverse=True)
            selected = scored[:top_k]

            blocks = []
            hits = []
            used_chars = 0
            for sc, ch in selected:
                block = f"【参考片段 - {ch['doc_name']}】\n{ch['text']}"
                if used_chars + len(block) > max_chars:
                    remain = max_chars - used_chars
                    if remain > 100:
                        blocks.append(block[:remain] + "...")
                        hits.append({
                            "doc_name": ch["doc_name"],
                            "chunk_idx": ch["idx"],
                            "score": round(sc, 2),
                            "text": ch["text"][:200] + ("..." if len(ch["text"]) > 200 else ""),
                        })
                    break
                blocks.append(block)
                used_chars += len(block) + 2
                hits.append({
                    "doc_name": ch["doc_name"],
                    "chunk_idx": ch["idx"],
                    "score": round(sc, 2),
                    "text": ch["text"],
                })

            return {
                "mode": "bm25",
                "context": "\n\n".join(blocks),
                "hits": hits,
                "total_enabled_docs": len(enabled_docs),
                "total_enabled_chars": total_chars,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            }

    def _reconstruct_doc_text(self, doc):
        """小文档直通模式优先命中内存缓存，未命中时读取原文本副本并缓存"""
        doc_id = doc.get("id")
        if doc_id and doc_id in self._doc_text_cache:
            return self._doc_text_cache[doc_id]

        text = ""
        rel = doc.get("stored_path")
        if rel:
            abs_p = os.path.join(self.kb_dir, rel)
            if os.path.exists(abs_p):
                try:
                    text = extract_text_from_file(abs_p)
                except Exception:
                    text = ""
        if not text:
            # 兜底用切片拼接
            text = "\n".join(ch.get("text", "") for ch in doc.get("chunks", []) if ch.get("text"))
        if doc_id:
            self._doc_text_cache[doc_id] = text
        return text


# =============================================================================
# 4. 模块级单例与 Prompt 拼装助手 (后台与 GUI 共用)
# =============================================================================
_DEFAULT_MANAGER = KnowledgeBaseManager()


def get_manager():
    return _DEFAULT_MANAGER


def retrieve_for_query(query, cfg=None):
    return _DEFAULT_MANAGER.retrieve(query, cfg=cfg)


def build_kb_prompt_block(kb_result):
    """
    将检索结果格式化为“去客服腔、自然口语化”的 System Prompt 附加段落。
    若无有效参考资料，返回空字符串。
    """
    if not isinstance(kb_result, dict):
        return ""
    ctx = (kb_result.get("context") or "").strip()
    if not ctx:
        return ""
    return (
        f"\n【主人备忘与个人资料（仅供参考）】：\n"
        f"{ctx}\n"
        f"【资料使用铁律】：以上内容是你（号主本人）自己记住的真实信息。"
        f"如果好友当前的问题与上述信息有关，请自然融入你的人设与日常口语直接回答，价格/时间/规则等关键事实必须以资料为准；"
        f"如果与当前聊天无关（如普通寒暄、闲聊），请完全忽略上述资料正常聊天。"
        f"严禁说出“根据资料/根据知识库/文档显示”等暴露机器人或客服身份的字眼。"
    )
