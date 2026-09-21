# Chaekgalpi Tools – EPUB Font Unlock

[한국어](README.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md)

Removes hard-coded fonts, font sizes and line spacing from EPUB files so the font settings on your e-reader actually apply. Not a single character of the book's text is changed. No server, no install, the original file is never modified.

> **DRM-protected EPUBs are not processed.** Most books bought from a store are. This tool neither removes nor works around DRM; it reports the file and skips it.

![Before and after](docs/before_after.png)

The same reader setting (Malgun Gothic, 21px, line spacing 2.0) applied to both sides. On the left the book's own CSS wins and the setting does nothing; on the right it gets through. The heading stays proportionally larger than the body text in both. Rendered from `samples/sample_epub2.epub` actually run through the tool; rebuild it with `python docs/make_before_after.py`.

## Download

- **Executable**: grab `epub-font-unlock.exe` from [Releases](https://github.com/microhan1/epub-font-unlock/releases) and double-click it. No installation.
- **From source**:

```bash
pip install -r requirements.txt
python main.py
```

## Usage

1. Drop EPUB files or a folder onto the window.
2. **Analysis** shows what is pinned down first, like "3 fixed fonts, 12 absolute sizes, 4 absolute line heights".
3. Check what to release and press **Run**. `<name>_unlocked.epub` appears beside the original.

Font, size and line height are ticked by default. Margin cleanup and removing embedded font files are not.

```bash
python main.py book.epub --font --size --line-height
python main.py my-books --analyze-only
python main.py book.epub --font --size --line-height --margin --remove-font-files
```

`python main.py --help` prints the options in your OS language (한국어 · English · 中文 · 日本語).

## What is removed

| Item | Removed | Kept |
|---|---|---|
| Font | `font-family` | Embedded font files and `@font-face` (optional removal) |
| Font size | Absolute `font-size` in px, pt, cm and the like | Relative values: `em`, `%`, `rem`, `larger` |
| Line height | Absolute `line-height` | Unitless numbers, `%`, `em` |
| Margins (optional) | Absolute `margin`/`padding` on `p` and `div` | `0`, relative values, table and figure spacing |

A heading staying larger than the body text is a relative promise, and it survives. The same rules apply to CSS files, to `<style>` blocks inside XHTML, and to `style="..."` attributes on tags.

## What it does not do

- It does not change the body text.
- It does not open DRM-protected EPUBs. It reports them and moves on.
- It does not convert between EPUB 2 and EPUB 3, or restructure the book.
- It does not touch the cover, the table of contents (NCX or nav), or the metadata (OPF). The one exception: when you remove embedded font files, the OPF manifest items pointing at those files go too, because a manifest entry for a missing file fails epubcheck.

## Series

- Chaekgalpi Tools: [Scan PDF Cleanup](https://github.com/microhan1/scan-pdf-cleanup) · [Margin Crop](https://github.com/microhan1/scan-pdf-crop) · [Two-page Split](https://github.com/microhan1/scan-pdf-split)
- [Chaekgalpi Library](https://github.com/microhan1/chaekgalpi)

## License

MIT. See [LICENSE](LICENSE).
