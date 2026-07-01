# E-Hentai 画廊下载器

批量下载 e-hentai.org 画廊，自动打包为 CBZ，支持搜索、断点续传、跳过已下载。

## 快速开始

**双击 `ehentai_downloader.bat`** 即可启动交互模式。

或命令行：

```bash
# 交互模式
python ehentai_downloader.py -i

# 搜索并下载全部
python ehentai_downloader.py --search "关键词" --all

# 搜索并下载指定
python ehentai_downloader.py --search "关键词" --pick 1,3,5-8

# 只列出不下载
python ehentai_downloader.py --search "关键词" --list

# 直接下载单个画廊
python ehentai_downloader.py --url "https://e-hentai.org/g/3888322/12d2ec9055/"
```

## 功能

- **搜索下载** — 按关键词搜索，选择全部/范围/单个下载
- **CBZ 打包** — 下载完成后自动打包为 `.cbz`（标准 ZIP 格式，可用 CDisplayEx、MComix 等阅读器打开）
- **断点续传** — 已下载的图片自动跳过，中断后重新运行会继续
- **跳过已完成** — 完整的 CBZ 不会重复下载
- **自动清理** — 打包成功后自动删除临时图片文件夹
- **日文标题** — 优先使用日文标题作为文件夹/CBZ 名称
- **代理支持** — e-hentai.org 走代理，hath.network CDN 直连（避免 SSL 问题）

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-i, --interactive` | 交互模式 | - |
| `-s, --search` | 搜索关键词 | - |
| `-u, --url` | 直接下载单个画廊 URL | - |
| `-o, --out` | 输出目录 | `./ehentai_cbz` |
| `--all` | 下载搜索结果全部 | - |
| `--pick` | 按序号下载，如 `1,3,5` 或 `1-4` | - |
| `--list` | 只列出不下载 | - |
| `--delay` | 请求间隔秒数 | `0.3` |
| `--proxy` | HTTP 代理地址 | `http://127.0.0.1:7897` |
| `--no-proxy` | 禁用代理 | - |
| `--overwrite` | 重新下载已有内容 | - |
| `--keep-folder` | 打包后保留图片文件夹 | - |

## 交互模式用法

```
请输入搜索关键词（直接输入画廊 URL 或关键词，Q=退出）：笠間しろう

找到 18 个画廊：
  01. [244页] [笠間しろう] 人妻禁忌 [中国翻訳]
  02. [39页] [笠間しろう] 凌辱の縄遊び [中国翻訳]
  ...

选择下载：
  A = 下载全部
  序号 = 下载单个，例如 3
  多个 = 例如 1,3,5 或 1-4
请输入选择：1,3,5-8
```

## 依赖

```bash
pip install requests
```

另外需要 `curl`（Windows 10/11 自带）用于下载 hath.network CDN 的图片。

## 注意事项

- 需要代理访问 e-hentai.org，默认使用 `http://127.0.0.1:7897`
- hath.network CDN 图片通过直连下载（走代理会有 SSL 错误）
- 遇到下载失败时调大 `--delay`，如 `--delay 1` 或 `--delay 2`
- 搜索结果中的中文翻译版（`[中国翻訳]`）和日文原版是不同的画廊
