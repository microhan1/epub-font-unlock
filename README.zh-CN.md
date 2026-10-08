# 书签工具 – EPUB字体解锁

[한국어](README.md) · [English](README.en.md) · [日本語](README.ja.md)

删除 EPUB 中固定的字体、字号和行距，让电子书阅读器里的字体设置真正生效。正文一个字也不改。无服务器，无需安装，绝不修改原文件。

> **不处理有 DRM 保护的 EPUB。** 在书店购买的书大多如此。本工具既不破解也不绕过 DRM，只会提示并跳过。不过 Sigil、InDesign 嵌入字体时使用的**字体混淆**不属于 DRM，这类书会正常处理。

![处理前后](docs/before_after.png)

两侧应用了完全相同的阅读器设置（맑은 고딕 · 21px · 行距 2.0）。左侧书中的 CSS 占了上风，设置不起作用；右侧设置则正常生效。标题比正文大的比例在两侧都保留了下来。该图由 `samples/sample_epub2.epub` 实际处理后渲染而成，可用 `python docs/make_before_after.py` 重新生成。

## 下载

- **可执行文件**：在 [Releases](https://github.com/microhan1/epub-font-unlock/releases) 下载 `epub-font-unlock.exe`，双击即可运行，无需安装。
- **从源码运行**：

```bash
pip install -r requirements.txt
python main.py
```

## 使用方法

1. 把 EPUB 文件或文件夹拖放到窗口中。
2. **分析结果**会先显示书中固定了什么，例如“固定字体 3 种，绝对字号 12 处，绝对行距 4 处”。
3. 确认要解除的项目后按**运行**，原文件旁会生成 `<原文件名>_unlocked.epub`。

默认只勾选字体、字号和行距三项。整理边距和删除内嵌字体文件默认关闭。

```bash
python main.py book.epub --font --size --line-height
python main.py 书籍文件夹 --analyze-only
python main.py book.epub --font --size --line-height --margin --remove-font-files
```

`python main.py --help` 会用操作系统语言（한국어 · English · 中文 · 日本語）显示选项。

## 删除的内容

| 项目 | 删除 | 保留 |
|---|---|---|
| 字体 | `font-family` | 内嵌字体文件和 `@font-face`（可选删除） |
| 字号 | `font-size` 的 px、pt、cm 等绝对值 | `em`、`%`、`rem`、`larger` 等相对值 |
| 行距 | `line-height` 的绝对值 | 无单位数字、`%`、`em` |
| 边距（可选） | `p`、`div` 的 `margin`、`padding` 绝对值 | `0`、相对值、`auto`（居中）、表格和插图的边距 |

标题比正文大这个约定（相对值）在处理后依然保留。CSS 文件、XHTML 中的 `<style>` 块，以及标签上的 `style="..."` 属性都适用同一套规则。

## 不做的事

- 不改动正文文字。
- 不打开有 DRM 保护的 EPUB，只提示并跳过。即使存在 `META-INF/encryption.xml`，只有其中全部是字体混淆时才视为非 DRM；只要混入任何其他方式，或文件无法读取，就按 DRM 处理并跳过。
- 不做 EPUB2 与 EPUB3 之间的转换，也不重建结构。
- 不改动封面、目录（NCX、nav）和元数据（OPF）。唯一的例外：删除内嵌字体文件时，会一并删除 OPF manifest 和 `encryption.xml` 中指向这些文件的条目，否则无法通过 epubcheck。

## 系列

- 书签工具：[扫描PDF清晰化](https://github.com/microhan1/scan-pdf-cleanup) · [裁边](https://github.com/microhan1/TrimPDF) · [分页](https://github.com/microhan1/scan-pdf-split)
- [书签图书馆（Chaekgalpi Library）](https://chaekgalpi.co.kr/?utm_source=github&utm_medium=referral&utm_campaign=tool_cta&utm_content=epubfont) — 记录读过的书和读书笔记的网页服务（仅韩语）

## 许可证

MIT。参见 [LICENSE](LICENSE)。

发布的 exe 还包含 Python、Tcl/Tk 等第三方组件，组件及其许可证全文见 [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt)。
