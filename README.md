# 책갈피 툴 – EPUB 글꼴 풀기

[English](README.en.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md)

EPUB 안에 고정된 글꼴·글자 크기·줄간격을 지워, 리디페이퍼·크레마·킨들에서 바꾼 글꼴 설정이 그대로 먹게 만듭니다. 본문 텍스트는 한 글자도 바꾸지 않습니다. 서버 없음, 설치 없음, 원본 무수정.

> **DRM이 걸린 EPUB은 처리하지 않습니다.** 서점에서 산 책 대부분이 여기에 해당합니다. DRM을 풀지도, 우회하지도 않고 안내만 하고 건너뜁니다.

![전후 비교](docs/before_after.png)

같은 리더기 설정(맑은 고딕 · 21px · 줄간격 2.0)을 양쪽에 똑같이 적용한 화면입니다. 왼쪽은 책 안의 CSS가 이겨서 설정이 먹지 않고, 오른쪽은 설정이 그대로 적용됩니다. 제목이 본문보다 크다는 비율은 양쪽 모두 남아 있습니다. `samples/sample_epub2.epub`을 실제로 처리해 렌더링한 것이고, `python docs/make_before_after.py`로 다시 만들 수 있습니다.

## 다운로드

- **실행 파일**: [Releases](https://github.com/microhan1/epub-font-unlock/releases)에서 `epub-font-unlock.exe`를 받아 더블클릭. 설치 없이 바로 실행됩니다.
- **소스 실행**:

```bash
pip install -r requirements.txt
python main.py
```

## 사용법

1. EPUB 파일이나 폴더를 창에 끌어다 놓습니다.
2. **분석 결과**에 "고정 글꼴 3종, 절대 크기 12곳, 절대 줄간격 4곳"처럼 무엇이 고정돼 있는지 먼저 표시됩니다.
3. 해제할 항목을 확인하고 **실행**을 누르면 원본 옆에 `<원본명>_unlocked.epub`이 만들어집니다.

기본값은 글꼴·크기·줄간격 세 개만 켜져 있습니다. 여백 정리와 내장 폰트 파일 삭제는 꺼져 있습니다.

```bash
python main.py book.epub --font --size --line-height
python main.py 책폴더 --analyze-only
python main.py book.epub --font --size --line-height --margin --remove-font-files
```

`python main.py --help`가 OS 언어(한국어 · English · 中文 · 日本語)로 옵션을 보여줍니다.

## 무엇을 지우는지

| 항목 | 지우는 것 | 남기는 것 |
|---|---|---|
| 글꼴 | `font-family` | 내장 폰트 파일과 `@font-face` (옵션으로 삭제) |
| 글자 크기 | `font-size`의 px·pt·cm 같은 절대값 | `em`, `%`, `rem`, `larger` 같은 상대값 |
| 줄간격 | `line-height`의 절대값 | 단위 없는 숫자, `%`, `em` |
| 여백 (옵션) | `p`, `div`의 `margin`·`padding` 절대값 | `0`, 상대값, `auto`(가운데 정렬), 표·그림의 여백 |

제목이 본문보다 크다는 약속(상대값)은 처리 후에도 그대로 남습니다. CSS 파일, XHTML 안의 `<style>` 블록, 그리고 태그의 `style="..."` 속성에 같은 규칙을 적용합니다.

## 하지 않는 것

- 본문 텍스트는 바꾸지 않습니다.
- DRM 걸린 EPUB은 열지 않습니다. 안내만 표시합니다.
- EPUB2↔EPUB3 변환이나 구조 재작성은 하지 않습니다.
- 표지, 목차(NCX·nav), 메타데이터(OPF)는 건드리지 않습니다. 내장 폰트 파일을 삭제할 때만, 사라진 파일을 가리키는 OPF manifest 항목을 함께 지웁니다. 그러지 않으면 epubcheck를 통과하지 못하기 때문입니다.

## 시리즈

- 책갈피 툴: [스캔 PDF 보정](https://github.com/microhan1/scan-pdf-cleanup) · [여백 자르기](https://github.com/microhan1/scan-pdf-crop) · [두쪽 나누기](https://github.com/microhan1/scan-pdf-split)
- [책갈피 라이브러리](https://github.com/microhan1/chaekgalpi)

## 라이선스

MIT. [LICENSE](LICENSE) 참조.

배포하는 exe에는 Python, Tcl/Tk 등 제3자 구성 요소가 함께 들어 있습니다. 구성 요소와 라이선스 전문은 [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt)에 있습니다.
