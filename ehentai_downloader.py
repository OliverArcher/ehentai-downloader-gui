#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
e-hentai 画廊下载打包脚本（通用版）

功能：
- 按关键词搜索 / 直接输入画廊 URL
- 可下载全部画廊，或只下载指定序号/范围
- 每个画廊保存为独立文件夹，并打包成 CBZ
- 支持断点续传：已下载且大小正常的图片会跳过
- 下载全部成功后，默认删除临时图片文件夹，只保留 CBZ
- hath.network 直连下载（绕过代理 SSL 问题），e-hentai.org 走代理

用法示例：
  python ehentai_downloader.py -i                          # 交互模式
  python ehentai_downloader.py --url <画廊URL> --all       # 直接下载指定画廊
  python ehentai_downloader.py --search "关键词" --all     # 搜索并下载全部
  python ehentai_downloader.py --search "关键词" --pick 1,3,5  # 下载指定序号
  python ehentai_downloader.py --search "关键词" --list    # 只列出不下载
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import requests
except ImportError:
    print("缺少依赖，请先运行：pip install requests", file=sys.stderr)
    raise

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
MIN_VALID_BYTES = 5000
PROXY = "http://127.0.0.1:7897"


# ─── 工具函数 ─────────────────────────────────────────────

def safe_name(s: str, max_len: int = 100) -> str:
    s = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", (s or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:max_len].rstrip(" .") or "untitled")


def is_valid_file(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= MIN_VALID_BYTES


def parse_pick(s: str, total: int) -> list[int]:
    nums = []
    for part in re.split(r"[,，\s]+", s.strip()):
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                a, b = int(a), int(b)
            except ValueError:
                print(f"[警告] 无效范围：{part}，已跳过")
                continue
            nums.extend(range(a, b + 1))
        else:
            try:
                nums.append(int(part))
            except ValueError:
                print(f"[警告] 无效序号：{part}，已跳过")
                continue
    invalid = [n for n in nums if not (1 <= n <= total)]
    if invalid:
        print(f"[警告] 以下序号超出范围 (1-{total})：{invalid}")
    return sorted(set(n for n in nums if 1 <= n <= total))


# ─── e-hentai 操作 ─────────────────────────────────────────

def create_session(proxy: str = PROXY) -> requests.Session:
    session = requests.Session()
    session.proxies = {"http": proxy, "https": proxy}
    session.headers.update({"User-Agent": UA})
    return session


def search_galleries(session: requests.Session, keyword: str, max_pages: int = 0) -> list[dict]:
    """搜索画廊，返回 [{gid, token, title, title_jpn, pages, url}, ...]"""
    seen = {}
    page = 0
    while True:
        url = f"https://e-hentai.org/?f_search={quote(keyword)}"
        if page > 0:
            url += f"&page={page}"

        for attempt in range(3):
            try:
                resp = session.get(url, timeout=30)
                resp.raise_for_status()
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    print(f"[错误] 搜索页面获取失败：{e}")
                    return list(seen.values())

        text = resp.text

        # 提取画廊链接: href="https://e-hentai.org/g/3888322/12d2ec9055/"
        links = re.findall(r'href="(https://e-hentai\.org/g/(\d+)/([^/"]+)/?)"', text)
        if not links:
            break

        for full_url, gid, token in links:
            gid = int(gid)
            if gid not in seen:
                seen[gid] = {"gid": gid, "token": token, "url": full_url.rstrip("/")}

        # 检查是否有下一页
        # 分页格式: ?p=1, ?p=2, ...
        if f"p={page + 1}" not in text:
            break

        page += 1
        if max_pages and page >= max_pages:
            break
        time.sleep(0.3)

    return list(seen.values())


def get_gallery_metadata(session: requests.Session, galleries: list[dict]) -> list[dict]:
    """用 API 批量获取画廊元数据（标题、页数等）"""
    # API 每次最多 25 个
    for i in range(0, len(galleries), 25):
        batch = galleries[i:i + 25]
        gidlist = [[g["gid"], g["token"]] for g in batch]

        for attempt in range(3):
            try:
                resp = session.post(
                    "https://api.e-hentai.org/api.php",
                    json={"method": "gdata", "gidlist": gidlist, "namespace": 1},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    print(f"[错误] API 获取失败：{e}")
                    data = {"gmetadata": []}

        for meta in data.get("gmetadata", []):
            gid = meta["gid"]
            for g in galleries:
                if g["gid"] == gid:
                    g["title"] = meta.get("title", "")
                    g["title_jpn"] = meta.get("title_jpn", "")
                    g["pages"] = int(meta.get("filecount", 0))
                    g["tags"] = meta.get("tags", [])
                    break

        time.sleep(0.3)

    return galleries


def get_folder_name(gal: dict) -> str:
    """选择最佳文件夹名：日文 > 英文"""
    jpn = gal.get("title_jpn", "").strip()
    if jpn:
        return safe_name(jpn)
    return safe_name(gal.get("title", f"gallery_{gal['gid']}"))


def collect_image_page_links(session: requests.Session, gid: int, token: str, total_pages: int = 0) -> dict[int, str]:
    """从缩略图页面收集所有图片页链接。
    total_pages: 画廊总图片数（从 API 获取），用于精确计算缩略图页数。
    """
    base = f"https://e-hentai.org/g/{gid}/{token}/"
    all_links = {}

    # 每页 20 个缩略图，根据 total_pages 计算需要抓几页
    if total_pages > 0:
        thumb_pages = (total_pages + 19) // 20
    else:
        thumb_pages = 50  # fallback: 最多抓 50 页

    for page in range(thumb_pages):
        url = base if page == 0 else f"{base}?p={page}"
        for attempt in range(3):
            try:
                resp = session.get(url, timeout=30)
                resp.raise_for_status()
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    print(f"    [错误] 缩略图页 {page} 获取失败：{e}")
                    return all_links

        links = re.findall(r'href="(https://e-hentai\.org/s/\w+/\w+-(\d+))"', resp.text)
        if not links:
            break

        for link, page_num in links:
            pn = int(page_num)
            if pn not in all_links:
                all_links[pn] = link

        time.sleep(0.3)

    return all_links


def extract_image_url(session: requests.Session, page_url: str) -> str | None:
    """从图片页提取真实图片 URL"""
    for attempt in range(3):
        try:
            resp = session.get(page_url, timeout=30)
            m = re.search(r'id="img"[^>]*src="([^"]+)"', resp.text)
            if m:
                return m.group(1)
        except:
            time.sleep(1)
    return None


def download_image_direct(img_url: str, save_path: str, referer: str) -> bool:
    """用 curl 直连下载图片（绕过代理 SSL 问题）"""
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["curl", "-s", "-L", "--insecure",
                 "-H", f"Referer: {referer}",
                 "-H", f"User-Agent: {UA}",
                 "-o", save_path, "--max-time", "120", img_url],
                capture_output=True, text=True, timeout=130
            )
            if os.path.exists(save_path) and os.path.getsize(save_path) > MIN_VALID_BYTES:
                return True
            time.sleep(2)
        except Exception:
            time.sleep(2)
    return False


def cbz_album(cbz_path: Path, files: list[Path]) -> None:
    """打包成 CBZ（本质上就是 ZIP）"""
    if cbz_path.exists():
        cbz_path.unlink()
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_STORED) as zf:
        for fp in files:
            if is_valid_file(fp):
                zf.write(fp, arcname=fp.name)


# ─── 核心下载逻辑 ──────────────────────────────────────────

def download_gallery(session: requests.Session, gal: dict, out_dir: Path,
                     delay: float, overwrite: bool, keep_folder: bool) -> Path | None:
    """下载单个画廊，返回 CBZ 路径"""
    gid = gal["gid"]
    token = gal["token"]
    folder_name = get_folder_name(gal)
    total_expected = gal.get("pages", 0)
    gal_dir = out_dir / folder_name
    cbz_path = out_dir / f"{folder_name}.cbz"

    # ── 检查是否已下载 ──
    if cbz_path.exists() and not overwrite:
        try:
            with zipfile.ZipFile(cbz_path, "r") as zf:
                cbz_count = len([n for n in zf.namelist() if re.search(r"\.(jpe?g|png|webp)$", n, re.I)])
            if total_expected and cbz_count >= total_expected:
                print(f"  [跳过] CBZ 已完整：{cbz_path.name}（{cbz_count}/{total_expected}）")
                if not keep_folder and gal_dir.exists():
                    shutil.rmtree(gal_dir, ignore_errors=True)
                return cbz_path
            print(f"  [续传] CBZ 不完整：{cbz_count}/{total_expected}，继续补齐")
        except Exception:
            print(f"  [续传] CBZ 无法校验，将补齐并重新打包")

    gal_dir.mkdir(parents=True, exist_ok=True)

    # ── 收集图片页链接 ──
    print(f"  收集图片页链接...")
    page_links = collect_image_page_links(session, gid, token, total_expected)
    if not page_links:
        print(f"  [错误] 未找到图片链接")
        return None
    print(f"  找到 {len(page_links)} 个图片页")

    # ── 找出缺失的页 ──
    missing = []
    for pn in range(1, len(page_links) + 1):
        candidates = list(gal_dir.glob(f"{pn:03d}.*"))
        if candidates and any(is_valid_file(c) for c in candidates):
            continue
        missing.append(pn)

    if not missing and not overwrite:
        print(f"  所有图片已存在")
    else:
        if overwrite:
            missing = list(range(1, len(page_links) + 1))
        print(f"  需要下载：{len(missing)} 张")

        # ── 下载 ──
        success = 0
        fail = 0
        failed_pages = []

        for idx, pn in enumerate(missing):
            if pn not in page_links:
                print(f"    [{pn}] 无页面链接")
                fail += 1
                failed_pages.append(pn)
                continue

            page_url = page_links[pn]
            img_url = extract_image_url(session, page_url)
            if not img_url:
                print(f"    [{pn}] 无法提取图片 URL")
                fail += 1
                failed_pages.append(pn)
                continue

            # 确定扩展名
            ext = ".jpg"
            if ".png" in img_url:
                ext = ".png"
            elif ".webp" in img_url:
                ext = ".webp"
            save_path = str(gal_dir / f"{pn:03d}{ext}")

            # 直连下载（hath.network 走代理会 SSL 报错）
            if download_image_direct(img_url, save_path, page_url):
                success += 1
            else:
                fail += 1
                failed_pages.append(pn)

            if (idx + 1) % 20 == 0:
                print(f"    进度：{idx + 1}/{len(missing)}（{success} 成功，{fail} 失败）")

            time.sleep(delay)

        print(f"  下载完成：{success} 成功，{fail} 失败")
        if failed_pages:
            print(f"  失败页：{failed_pages[:20]}{'...' if len(failed_pages) > 20 else ''}")

    # ── 打包 CBZ ──
    all_files = sorted([
        f for f in gal_dir.iterdir()
        if f.is_file() and f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')
    ])

    if all_files:
        cbz_album(cbz_path, all_files)
        size_mb = cbz_path.stat().st_size / 1024 / 1024
        print(f"  [打包] {cbz_path.name}（{len(all_files)} 张，{size_mb:.1f} MB）")

        if not keep_folder:
            shutil.rmtree(gal_dir, ignore_errors=True)
            print(f"  [清理] 已删除图片文件夹：{folder_name}")
    else:
        print(f"  [警告] 没有有效图片，跳过打包")

    return cbz_path


def download_galleries(session: requests.Session, galleries: list[dict], out_dir: Path,
                       delay: float, overwrite: bool, keep_folder: bool) -> int:
    """批量下载，返回失败数"""
    failed_count = 0
    for i, gal in enumerate(galleries, 1):
        title = gal.get("title_jpn") or gal.get("title", f"gallery_{gal['gid']}")
        print(f"\n{'='*60}")
        print(f"[{i}/{len(galleries)}] {title}")
        print(f"  https://e-hentai.org/g/{gal['gid']}/{gal['token']}/")
        print(f"  预计 {gal.get('pages', '?')} 页")

        try:
            result = download_gallery(session, gal, out_dir, delay, overwrite, keep_folder)
            if result is None:
                failed_count += 1
        except KeyboardInterrupt:
            print("\n[中断] 已停止。下次运行会跳过已完成的画廊并继续。")
            raise
        except Exception as e:
            failed_count += 1
            print(f"  [错误] {e}", file=sys.stderr)

    return failed_count


# ─── 交互模式 ──────────────────────────────────────────────

def interactive(session: requests.Session, out_dir: Path, delay: float,
                overwrite: bool, keep_folder: bool) -> None:
    """交互式搜索 + 选择下载"""
    while True:
        print(f"\n{'='*60}")
        print("e-hentai 画廊下载器")
        print("="*60)
        kw = input("请输入搜索关键词（直接输入画廊 URL 或关键词，Q=退出）：").strip()
        if kw.lower() == "q":
            print("已退出。")
            return

        if not kw:
            continue

        # 判断是 URL 还是关键词
        if "e-hentai.org/g/" in kw:
            # 直接下载单个画廊
            m = re.search(r"e-hentai\.org/g/(\d+)/([^/]+)", kw)
            if not m:
                print("[错误] 无效的画廊 URL")
                continue
            gid, token = m.group(1), m.group(2)
            galleries = [{"gid": int(gid), "token": token, "url": kw.rstrip("/")}]
            galleries = get_gallery_metadata(session, galleries)
        else:
            # 搜索
            print(f"搜索中：{kw}")
            galleries = search_galleries(session, kw)
            if not galleries:
                print("未找到结果。")
                continue
            print(f"获取元数据...")
            galleries = get_gallery_metadata(session, galleries)

        # 列出结果
        print(f"\n找到 {len(galleries)} 个画廊：")
        for i, g in enumerate(galleries, 1):
            title = g.get("title_jpn") or g.get("title", "未知标题")
            pages = g.get("pages", "?")
            print(f"  {i:02d}. [{pages}页] {title}")

        # 选择
        print(f"\n选择下载：")
        print(f"  A = 下载全部")
        print(f"  序号 = 下载单个，例如 3")
        print(f"  多个 = 例如 1,3,5 或 1-4")
        print(f"  L = 只列出，不下载")
        print(f"  Q = 返回")

        ans = input("请输入选择：").strip()
        if ans.lower() == "q":
            continue
        if ans.lower() == "l":
            continue
        if ans.lower() == "a" or ans == "":
            selected = galleries
        else:
            idxs = parse_pick(ans, len(galleries))
            if not idxs:
                print("未选择任何画廊。")
                continue
            selected = [galleries[i - 1] for i in idxs]

        # 下载
        print(f"\n准备下载 {len(selected)} 个画廊，间隔 {delay} 秒")
        try:
            failed = download_galleries(session, selected, out_dir, delay, overwrite, keep_folder)
        except KeyboardInterrupt:
            continue

        print(f"\n{'='*60}")
        if failed:
            print(f"[完成] {len(selected) - failed} 成功，{failed} 失败")
        else:
            print(f"[完成] 全部 {len(selected)} 个画廊下载成功！")


# ─── 主函数 ────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="e-hentai 画廊下载打包脚本（通用版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  %(prog)s -i                              交互模式
  %(prog)s --search "关键词" --all         搜索并下载全部
  %(prog)s --search "关键词" --pick 1,3    下载指定序号
  %(prog)s --search "关键词" --list        只列出不下载
  %(prog)s --url <画廊URL>                 下载单个画廊
        """
    )
    ap.add_argument("-i", "--interactive", action="store_true", help="交互模式")
    ap.add_argument("--search", "-s", default="", help="搜索关键词")
    ap.add_argument("--url", "-u", default="", help="直接下载单个画廊 URL")
    ap.add_argument("--out", "-o", default=str(Path(__file__).resolve().parent / "ehentai_cbz"), help="输出目录")
    ap.add_argument("--all", action="store_true", help="下载搜索结果中的全部画廊")
    ap.add_argument("--pick", default="", help="按序号下载，如 1 或 1,3,5 或 1-4")
    ap.add_argument("--list", "--dry-run", dest="list_only", action="store_true", help="只列出搜索结果，不下载")
    ap.add_argument("--delay", type=float, default=0.3, help="请求间隔秒数（默认 0.3）")
    ap.add_argument("--proxy", default=PROXY, help=f"HTTP 代理地址（默认 {PROXY}）")
    ap.add_argument("--no-proxy", action="store_true", help="禁用代理")
    ap.add_argument("--overwrite", action="store_true", help="重新下载已有图片/CBZ")
    ap.add_argument("--keep-folder", action="store_true", help="打包后保留图片文件夹")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    proxy = "" if args.no_proxy else args.proxy
    session = create_session(proxy)

    if args.interactive:
        interactive(session, out_dir, args.delay, args.overwrite, args.keep_folder)
        return 0

    if args.url:
        m = re.search(r"e-hentai\.org/g/(\d+)/([^/]+)", args.url)
        if not m:
            print("[错误] 无效的画廊 URL")
            return 1
        gid, token = m.group(1), m.group(2)
        galleries = [{"gid": int(gid), "token": token, "url": args.url.rstrip("/")}]
        galleries = get_gallery_metadata(session, galleries)
    elif args.search:
        print(f"搜索：{args.search}")
        galleries = search_galleries(session, args.search)
        if not galleries:
            print("未找到结果。")
            return 1
        print(f"获取元数据...")
        galleries = get_gallery_metadata(session, galleries)
    else:
        ap.print_help()
        return 0

    # 列出结果
    print(f"\n找到 {len(galleries)} 个画廊：")
    for i, g in enumerate(galleries, 1):
        title = g.get("title_jpn") or g.get("title", "未知标题")
        pages = g.get("pages", "?")
        print(f"  {i:02d}. [{pages}页] {title}")

    if args.list_only:
        return 0

    # 选择画廊
    if args.pick:
        idxs = parse_pick(args.pick, len(galleries))
        galleries = [galleries[i - 1] for i in idxs]
    elif not args.all:
        print("\n未指定 --all 或 --pick，安全起见只列出不下载。")
        print(f"下载全部：python {sys.argv[0]} --search \"{args.search}\" --all")
        print(f"下载指定：python {sys.argv[0]} --search \"{args.search}\" --pick 1,3")
        return 0

    # 下载
    print(f"\n准备下载 {len(galleries)} 个画廊，间隔 {args.delay} 秒，输出：{out_dir}")
    try:
        failed = download_galleries(session, galleries, out_dir, args.delay, args.overwrite, args.keep_folder)
    except KeyboardInterrupt:
        return 130

    print(f"\n{'='*60}")
    if failed:
        print(f"[完成] {len(galleries) - failed} 成功，{failed} 失败")
        return 1
    else:
        print(f"[完成] 全部 {len(galleries)} 个画廊下载成功！")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
