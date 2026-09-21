"""Rebuild docs/before_after.png.

    python docs/make_before_after.py

The two panes are not a drawing. They are the real ch1.xhtml and style.css out
of samples/sample_epub2.epub -- left as it ships, right after the tool ran --
rendered by a real browser. The same reader setting is applied to both the way
an e-reader applies one: on html/body, inherited down. That is why the left
pane looks untouched (the book's own rules on p and h1 beat inheritance) and
the right pane follows the setting.

Needs Microsoft Edge or Google Chrome, and samples/sample_epub2.epub
(python samples/make_samples.py).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import unlock  # noqa: E402

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

READER_FONT = "Malgun Gothic"
READER_LABEL = "맑은 고딕"
READER_SIZE = "21px"
READER_LINE = "2.0"

READER_CSS = f"""
html, body {{
  font-family: "{READER_FONT}", sans-serif;
  font-size: {READER_SIZE};
  line-height: {READER_LINE};
}}
"""

PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<style>{reader}</style>
<style>{book}</style>
<style>
  html {{ background: #fff; }}
  body {{ padding: 26px 30px 30px; }}
</style>
</head><body>{body}</body></html>
"""

WRAP = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 26px;
    background: #eceef1;
    font-family: "{font}", sans-serif;
    color: #1b1d20;
  }}
  h1.hdr {{ margin: 0 0 4px; font-size: 21px; letter-spacing: -0.2px; }}
  p.sub {{ margin: 0 0 20px; font-size: 14px; color: #5b6069; }}
  p.sub b {{ color: #1b1d20; font-weight: 600; }}
  .cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }}
  .col {{ background: #fff; border-radius: 12px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(0,0,0,.12), 0 8px 24px rgba(0,0,0,.07); }}
  .cap {{ display: flex; align-items: baseline; gap: 9px;
          padding: 13px 18px; border-bottom: 1px solid #e4e7eb; }}
  .cap .tag {{ font-size: 12px; font-weight: 700; letter-spacing: .4px;
               padding: 3px 9px; border-radius: 999px; color: #fff; }}
  .before .tag {{ background: #b3261e; }}
  .after  .tag {{ background: #146c2e; }}
  .cap .txt {{ font-size: 14px; color: #3c4149; }}
  .cap .file {{ margin-left: auto; font-size: 12px; color: #8b9098;
                font-family: Consolas, monospace; }}
  iframe {{ display: block; width: 100%; height: 408px; border: 0; }}
</style>
</head><body>
  <h1 class="hdr">EPUB 글꼴 풀기 — 같은 리더기 설정, 같은 책</h1>
  <p class="sub">같은 리더기 설정 <b>{label} · {size} · 줄간격 {line}</b> 을 양쪽에 똑같이 적용했습니다.
     왼쪽은 책 안의 CSS가 이겨서 설정이 먹지 않습니다. 제목이 본문보다 크다는 비율은 양쪽 모두 남습니다.</p>
  <div class="cols">
    <div class="col before">
      <div class="cap"><span class="tag">전</span>
        <span class="txt">글꼴·크기·줄간격이 책에 고정돼 있음</span>
        <span class="file">sample_epub2.epub</span></div>
      <iframe src="before.html"></iframe>
    </div>
    <div class="col after">
      <div class="cap"><span class="tag">후</span>
        <span class="txt">리더기 설정이 그대로 적용됨</span>
        <span class="file">sample_epub2_unlocked.epub</span></div>
      <iframe src="after.html"></iframe>
    </div>
  </div>
</body></html>
"""

BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.S | re.I)


def find_browser() -> str:
    for path in BROWSERS:
        if os.path.exists(path):
            return path
    found = shutil.which("chrome") or shutil.which("msedge")
    if found:
        return found
    raise SystemExit("Edge or Chrome is needed to render the screenshot.")


def part(epub: str, name: str) -> str:
    with zipfile.ZipFile(epub) as zf:
        return zf.read(name).decode("utf-8")


def main() -> int:
    source = os.path.join(REPO, "samples", "sample_epub2.epub")
    if not os.path.exists(source):
        raise SystemExit("run samples/make_samples.py first")
    work = tempfile.mkdtemp(prefix="efu_shot_")
    try:
        book = shutil.copy(source, os.path.join(work, "sample_epub2.epub"))
        result = unlock.process_epub(book, unlock.Options())
        for label, path in (("before", book), ("after", result.output_path)):
            body = BODY_RE.search(part(path, "OEBPS/ch1.xhtml")).group(1)
            with open(os.path.join(work, f"{label}.html"), "w", encoding="utf-8") as f:
                f.write(PAGE.format(reader=READER_CSS, book=part(path, "OEBPS/style.css"), body=body))
        with open(os.path.join(work, "compare.html"), "w", encoding="utf-8") as f:
            f.write(WRAP.format(font=READER_FONT, label=READER_LABEL,
                                size=READER_SIZE, line=READER_LINE))

        target = os.path.join(HERE, "before_after.png")
        subprocess.run([
            find_browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=2", "--allow-file-access-from-files",
            f"--screenshot={target}", "--window-size=1320,600",
            "file:///" + os.path.join(work, "compare.html").replace("\\", "/"),
        ], capture_output=True, text=True, timeout=180)
        if not os.path.exists(target):
            raise SystemExit("the browser did not write a screenshot")
        print("wrote", target, os.path.getsize(target), "bytes")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
