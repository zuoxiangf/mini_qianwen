# backend/extract.py
# 本模块职责：把上传文件 / 网页正文统一抽取成纯文本，供 /api/kb/upload 与 /api/kb/web 入库。
# 支持：纯文本（编码嗅探：UTF-8 → GBK → 自动探测）、PDF、DOCX、HTML 网页。
# 解析库采用软导入：缺哪个库只影响对应格式，其余格式与文本编码嗅探仍可用。
import os
import logging

log = logging.getLogger("extract")

# 单文件抽取后的文本上限，防止超大文档撑爆内存 / token
MAX_TEXT_CHARS = int(os.environ.get("KB_MAX_TEXT_CHARS", "2000000"))


def _sniff_decode(raw: bytes) -> str:
    """编码嗅探：先试 UTF-8，再试中文常见 GBK，最后用 charset-normalizer 兜底探测。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw).best()          # 可能为 None（二进制乱码）
        if best is not None:
            log.info("编码嗅探 | 探测结果=%s", best.encoding)
            return str(best)
    except ImportError:
        log.info("charset-normalizer 未安装，跳过自动探测")
    # 全部失败：保留可辨认字符、丢弃非法字节，避免直接报错中断入库
    log.warning("编码嗅探失败，按 UTF-8 宽松解码（可能有乱码）")
    return raw.decode("utf-8", errors="replace")


def extract_text(raw: bytes, filename: str = "") -> str:
    """按扩展名分流抽取纯文本；空内容或超限由调用方检查。

    不支持的扩展名一律退回编码嗅探，按文本处理（尽量宽容，不误伤未知后缀）。
    """
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    text = None
    if ext == "pdf":
        text = _extract_pdf(raw)
    elif ext in ("docx", "doc"):
        text = _extract_docx(raw, ext)
    elif ext in ("html", "htm"):
        text = _html_to_text(_sniff_decode(raw))
    if text is None:
        text = _sniff_decode(raw)
    if len(text) > MAX_TEXT_CHARS:
        log.warning("文本超限截断 | 原长度=%d | 上限=%d", len(text), MAX_TEXT_CHARS)
        text = text[:MAX_TEXT_CHARS]
    return text.strip()


def _extract_pdf(raw: bytes) -> str:
    """pypdf 逐页抽取文本，页间用空行拼接；扫描版/图片型 PDF 会抽到空文本（需 OCR）。"""
    try:
        import io
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("服务器未安装 pypdf，无法解析 PDF")
    reader = PdfReader(io.BytesIO(raw))
    pages = [p.extract_text() or "" for p in reader.pages]
    chars = sum(len(p) for p in pages)
    log.info("PDF 解析 | 页数=%d | 抽取字符=%d", len(pages), chars)
    if chars == 0:
        # 扫描版/图片型 PDF 在这一步就会抽出空文本，若不留 warning 就会被当成“入库成功但库里没东西”
        log.warning("PDF 无可提取文本（共 %d 页）：大概率为扫描版/图片型，需 OCR 后重试", len(pages))
    return "\n\n".join(pages)


def _extract_docx(raw: bytes, ext: str) -> str:
    """python-docx 按段落抽取正文，过滤空段；旧版二进制 .doc 格式不支持，提前拦截。"""
    if ext == "doc":
        raise ValueError("旧版 .doc 暂不支持，请转存为 .docx 后上传")
    try:
        import io
        import docx
    except ImportError:
        raise RuntimeError("服务器未安装 python-docx，无法解析 DOCX")
    doc = docx.Document(io.BytesIO(raw))
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    log.info("DOCX 解析 | 段落数=%d", len(paras))
    return "\n".join(paras)


def _html_to_text(html: str) -> str:
    """抽取网页正文：去 script/style，按块级标签换行，去 HTML 实体。"""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("服务器未安装 beautifulsoup4，无法解析 HTML")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # 压缩连续空行，保留段落结构
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)
