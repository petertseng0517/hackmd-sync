#!/usr/bin/env python3
"""
HackMD 筆記同步工具
將 HackMD 所有筆記下載為本地 .md 檔案，並盡量保留目錄結構。

使用方式：
  1. 設定環境變數 HACKMD_TOKEN，或直接在腳本中填入 token
  2. 執行: python3 hackmd_sync.py
     可選參數:
       --output-dir   輸出目錄（預設: ./hackmd_notes）
       --include-teams 也下載所有 Team workspace 的筆記

取得 HackMD API Token：
  HackMD → 右上角頭像 → Settings → API → 建立新 token
"""

import os
import re
import sys
import time
import argparse
import requests
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── 設定區 ──────────────────────────────────────────────
HACKMD_TOKEN = os.environ.get("HACKMD_TOKEN", "")
API_BASE = "https://api.hackmd.io/v1"
RATE_LIMIT_DELAY = 1.2  # 每次 API 請求之間的間隔秒數（避免被限速）
# ────────────────────────────────────────────────────────


def make_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    return session


def sanitize_filename(name: str) -> str:
    """將筆記標題轉成合法的檔案名稱"""
    # 去掉不能用在檔名的字元
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    # 去掉前後空白與點
    name = name.strip(". ")
    # 避免空字串
    return name or "untitled"


def get_safe_path(base_dir: Path, relative_parts: list[str], title: str) -> Path:
    """組合輸出路徑，確保 .md 副檔名"""
    parts = [sanitize_filename(p) for p in relative_parts if p]
    filename = sanitize_filename(title) + ".md"
    return base_dir.joinpath(*parts, filename)


def avoid_collision(path: Path) -> Path:
    """若檔案已存在則在檔名後加流水號"""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    i = 1
    while True:
        new_path = parent / f"{stem}_{i}{suffix}"
        if not new_path.exists():
            return new_path
        i += 1


# ── API 呼叫 ────────────────────────────────────────────

def api_get(session: requests.Session, path: str, max_retries: int = 5) -> dict | list:
    url = f"{API_BASE}{path}"
    wait = 10  # 初始等待秒數
    for attempt in range(max_retries):
        resp = session.get(url)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", wait))
            actual_wait = max(retry_after, wait)
            print(f"  [限速] 等待 {actual_wait} 秒後重試（第 {attempt + 1} 次）...")
            time.sleep(actual_wait)
            wait = min(wait * 2, 120)  # 指數退避，最多等 120 秒
            continue
        resp.raise_for_status()
        time.sleep(RATE_LIMIT_DELAY)
        return resp.json()
    raise Exception(f"超過最大重試次數（{max_retries}），仍被限速")


def fetch_note_content(session: requests.Session, note_id: str) -> dict:
    return api_get(session, f"/notes/{note_id}")


def fetch_my_notes(session: requests.Session) -> list[dict]:
    return api_get(session, "/notes")


def fetch_teams(session: requests.Session) -> list[dict]:
    return api_get(session, "/teams")


def fetch_team_notes(session: requests.Session, team_path: str) -> list[dict]:
    return api_get(session, f"/teams/{team_path}/notes")


# ── 下載邏輯 ────────────────────────────────────────────

def build_frontmatter(note: dict) -> str:
    """將筆記 metadata 轉成 YAML frontmatter"""
    lines = ["---"]
    if note.get("title"):
        lines.append(f'title: "{note["title"]}"')
    if note.get("tags"):
        lines.append("tags:")
        for tag in note["tags"]:
            lines.append(f"  - {tag}")
    if note.get("createdAt"):
        ts = note["createdAt"] / 1000
        lines.append(f'created: {datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")}')
    if note.get("lastChangedAt"):
        ts = note["lastChangedAt"] / 1000
        lines.append(f'updated: {datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")}')
    if note.get("permalink"):
        lines.append(f'hackmd_url: "https://hackmd.io/{note["permalink"]}"')
    elif note.get("id"):
        lines.append(f'hackmd_id: "{note["id"]}"')
    lines.append("---\n")
    return "\n".join(lines)


def save_note(note_detail: dict, output_path: Path, add_frontmatter: bool = True):
    content = note_detail.get("content", "")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        if add_frontmatter:
            f.write(build_frontmatter(note_detail))
        f.write(content)


def infer_folder_from_tags(tags: list[str]) -> list[str]:
    """
    若 HackMD 無資料夾資訊，嘗試從 tags 推斷一層子目錄。
    只取第一個 tag 作為資料夾（可依需求調整）。
    """
    if tags:
        return [tags[0]]
    return []


def build_local_index(output_dir: Path) -> dict[str, Path]:
    """掃描 output_dir 下所有 .md，回傳 {note_id: file_path}"""
    index: dict[str, Path] = {}
    for md_file in output_dir.rglob("*.md"):
        if md_file.name == "sync_failures.log":
            continue
        nid = parse_frontmatter_id(md_file)
        if nid:
            index[nid] = md_file
    return index


def download_personal_notes(session, output_dir: Path, add_frontmatter: bool) -> list[dict]:
    """回傳失敗清單，每項為 {title, note_id, error}"""
    print("\n── 個人筆記 ──")
    notes = fetch_my_notes(session)
    print(f"共 {len(notes)} 篇個人筆記")

    # 建立本地索引，key = hackmd_id，value = 本地檔案路徑
    local_index = build_local_index(output_dir)

    saved = skipped = 0
    failures: list[dict] = []
    for i, note in enumerate(notes, 1):
        note_id = note["id"]
        title = note.get("title") or note_id
        remote_ts = (note.get("lastChangedAt") or 0) / 1000

        # 若本地已有此筆記，比對時間戳
        if note_id in local_index:
            local_path = local_index[note_id]
            local_mtime = local_path.stat().st_mtime
            if remote_ts <= local_mtime + 1:  # +1 秒容差
                print(f"  [{i}/{len(notes)}] {title} ... 略過（已是最新）")
                skipped += 1
                continue

        print(f"  [{i}/{len(notes)}] {title}", end=" ... ", flush=True)
        try:
            detail = fetch_note_content(session, note_id)

            # HackMD 個人筆記無資料夾概念，以 tags[0] 當一層子目錄
            tags = detail.get("tags") or note.get("tags") or []
            folder_parts = infer_folder_from_tags(tags)

            if note_id in local_index:
                # 覆蓋原檔（HackMD 較新）
                out_path = local_index[note_id]
            else:
                out_path = get_safe_path(output_dir / "個人筆記", folder_parts, title)
                out_path = avoid_collision(out_path)

            save_note(detail, out_path, add_frontmatter)
            print(f"OK → {out_path.relative_to(output_dir)}")
            saved += 1
        except Exception as e:
            err = str(e)
            print(f"FAIL ({err})")
            failures.append({"title": title, "note_id": note_id, "error": err})

    print(f"個人筆記：已更新 {saved} / 略過 {skipped} / 失敗 {len(failures)}")
    return failures


def download_team_notes(session, output_dir: Path, add_frontmatter: bool) -> list[dict]:
    """回傳失敗清單，每項為 {title, note_id, error}"""
    print("\n── Team / Workspace 筆記 ──")
    failures: list[dict] = []
    local_index = build_local_index(output_dir)
    try:
        teams = fetch_teams(session)
    except requests.HTTPError as e:
        print(f"  無法取得 team 列表（{e}），略過。")
        return failures

    for team in teams:
        team_name = team.get("name") or team.get("path", "unknown_team")
        team_path = team.get("path")
        print(f"\n  Team: {team_name} ({team_path})")

        try:
            notes = fetch_team_notes(session, team_path)
        except requests.HTTPError as e:
            print(f"    無法取得筆記列表（{e}），略過此 team。")
            continue

        print(f"  共 {len(notes)} 篇")
        saved = skipped = 0

        for i, note in enumerate(notes, 1):
            note_id = note["id"]
            title = note.get("title") or note_id
            remote_ts = (note.get("lastChangedAt") or 0) / 1000

            if note_id in local_index:
                local_path = local_index[note_id]
                if remote_ts <= local_path.stat().st_mtime + 1:
                    print(f"    [{i}/{len(notes)}] {title} ... 略過（已是最新）")
                    skipped += 1
                    continue

            print(f"    [{i}/{len(notes)}] {title}", end=" ... ", flush=True)
            try:
                detail = fetch_note_content(session, note_id)

                permalink = detail.get("permalink") or ""
                path_parts = [p for p in permalink.split("/") if p][1:-1]

                if note_id in local_index:
                    out_path = local_index[note_id]
                else:
                    out_path = get_safe_path(
                        output_dir / sanitize_filename(team_name),
                        path_parts,
                        title,
                    )
                    out_path = avoid_collision(out_path)

                save_note(detail, out_path, add_frontmatter)
                print(f"OK → {out_path.relative_to(output_dir)}")
                saved += 1
            except Exception as e:
                err = str(e)
                print(f"FAIL ({err})")
                failures.append({"title": title, "note_id": note_id, "error": err})

        print(f"  {team_name}：已更新 {saved} / 略過 {skipped} / 失敗 {len([f for f in failures if True])}")

    return failures


# ── 比對邏輯 ────────────────────────────────────────────

def parse_frontmatter_id(md_path: Path) -> str | None:
    """從 .md 檔案的 frontmatter 取出 hackmd_id 或從 hackmd_url 解析 ID"""
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    frontmatter = text[3:end]
    for line in frontmatter.splitlines():
        if line.startswith("hackmd_id:"):
            return line.split(":", 1)[1].strip().strip('"')
        if line.startswith("hackmd_url:"):
            url = line.split(":", 1)[1].strip().strip('"')
            return url.rstrip("/").split("/")[-1]
    return None


def compare_notes(session, output_dir: Path):
    """比對 local 與 HackMD 端的筆記狀態"""
    print("\n── 比對筆記狀態 ──")

    # 1. 建立 HackMD 筆記 ID → metadata 的字典
    remote_notes = fetch_my_notes(session)
    remote_map: dict[str, dict] = {n["id"]: n for n in remote_notes}
    print(f"HackMD 端：{len(remote_map)} 篇")

    # 2. 掃描 local 所有 .md，解析 frontmatter 中的 hackmd_id
    local_files = list(output_dir.rglob("*.md"))
    # 排除 log 檔
    local_files = [f for f in local_files if f.name != "sync_failures.log"]
    print(f"Local 端：{len(local_files)} 個 .md 檔案")

    local_map: dict[str, Path] = {}  # note_id → 檔案路徑
    no_id_files: list[Path] = []
    for f in local_files:
        nid = parse_frontmatter_id(f)
        if nid:
            local_map[nid] = f
        else:
            no_id_files.append(f)

    # 3. 分類
    up_to_date: list[dict] = []
    remote_newer: list[dict] = []   # HackMD 比 local 新 → 需要更新
    local_only: list[Path] = []     # local 有但 HackMD 沒有（可能已刪除）
    remote_only: list[dict] = []    # HackMD 有但 local 沒有（尚未下載）

    for note_id, remote in remote_map.items():
        if note_id not in local_map:
            remote_only.append(remote)
            continue
        local_path = local_map[note_id]
        local_mtime = local_path.stat().st_mtime
        remote_ts = (remote.get("lastChangedAt") or 0) / 1000
        if remote_ts > local_mtime + 1:  # +1 秒容差
            remote_newer.append({
                "title": remote.get("title", note_id),
                "note_id": note_id,
                "local_path": local_path,
                "remote_updated": datetime.fromtimestamp(remote_ts).strftime("%Y-%m-%d %H:%M:%S"),
                "local_updated": datetime.fromtimestamp(local_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
        else:
            up_to_date.append(remote)

    for note_id, local_path in local_map.items():
        if note_id not in remote_map:
            local_only.append(local_path)

    # 4. 印出結果
    print(f"\n{'─'*50}")
    print(f"✓ 已是最新：{len(up_to_date)} 篇")

    if remote_newer:
        print(f"\n↓ HackMD 較新，需更新：{len(remote_newer)} 篇")
        for n in remote_newer:
            print(f"  • {n['title']}")
            print(f"      Local:  {n['local_updated']}  ({n['local_path'].relative_to(output_dir)})")
            print(f"      Remote: {n['remote_updated']}")

    if remote_only:
        print(f"\n+ 尚未下載（HackMD 有，local 無）：{len(remote_only)} 篇")
        for n in remote_only:
            print(f"  • {n.get('title', n['id'])}")

    if local_only:
        print(f"\n? Local 有但 HackMD 無（可能已刪除）：{len(local_only)} 個")
        for p in local_only:
            print(f"  • {p.relative_to(output_dir)}")

    if no_id_files:
        print(f"\n~ 無 hackmd_id（非從 HackMD 下載或未加 frontmatter）：{len(no_id_files)} 個")

    print(f"\n{'─'*50}")
    print("說明：")
    print("  ↓ 重新執行腳本（無 --compare）可更新 HackMD 較新的筆記")
    print("  + 重新執行腳本可下載尚未取得的筆記")
    print("  ? 可手動確認是否要刪除 local 檔案")


# ── 主程式 ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="下載 HackMD 所有筆記至本地 .md 檔案")
    parser.add_argument(
        "--output-dir", default="./hackmd_notes",
        help="輸出根目錄（預設: ./hackmd_notes）"
    )
    parser.add_argument(
        "--include-teams", action="store_true",
        help="同時下載所有 Team workspace 的筆記"
    )
    parser.add_argument(
        "--no-frontmatter", action="store_true",
        help="不加 YAML frontmatter，只存原始 markdown 內容"
    )
    parser.add_argument(
        "--token", default="",
        help="HackMD API token（也可設環境變數 HACKMD_TOKEN）"
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="比對 local 與 HackMD 端的筆記狀態（不下載）"
    )
    args = parser.parse_args()

    # 取得 token
    token = args.token or HACKMD_TOKEN
    if not token:
        print("錯誤：請提供 HackMD API token。")
        print("  方式一：執行時加上 --token YOUR_TOKEN")
        print("  方式二：設定環境變數 export HACKMD_TOKEN=YOUR_TOKEN")
        print("\n  取得 token：HackMD → Settings → API → 建立新 token")
        sys.exit(1)

    output_dir = Path(args.output_dir).resolve()
    add_frontmatter = not args.no_frontmatter

    print(f"輸出目錄：{output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    session = make_session(token)

    # 驗證 token
    try:
        me = api_get(session, "/me")
        print(f"登入帳號：{me.get('name', '')} ({me.get('email', '')})")
    except requests.HTTPError as e:
        print(f"Token 驗證失敗：{e}")
        sys.exit(1)

    if args.compare:
        compare_notes(session, output_dir)
        return

    all_failures: list[dict] = []
    all_failures += download_personal_notes(session, output_dir, add_frontmatter)

    if args.include_teams:
        all_failures += download_team_notes(session, output_dir, add_frontmatter)

    # ── 失敗匯總 ────────────────────────────────────────
    if all_failures:
        log_path = output_dir / "sync_failures.log"
        print(f"\n{'─'*50}")
        print(f"失敗筆記共 {len(all_failures)} 篇：")
        with open(log_path, "w", encoding="utf-8") as log:
            log.write(f"同步失敗記錄  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            log.write(f"{'─'*50}\n")
            for idx, f in enumerate(all_failures, 1):
                line = f"[{idx:02d}] {f['title']}  (ID: {f['note_id']})\n      原因: {f['error']}"
                print(f"  {line}")
                log.write(line + "\n")
        print(f"\n詳細記錄已存至：{log_path}")
        print("\n常見原因與處理方式：")
        print("  403 Forbidden  → 筆記為私人或已刪除，無法讀取")
        print("  404 Not Found  → 筆記不存在（可能已移除）")
        print("  429 Too Many Requests → 被 API 限速，稍後再用 --retry-failed 重試")
        print("  其他           → 網路問題，重新執行腳本即可（已下載的不會重複下載）")
    else:
        print("\n所有筆記下載成功！")

    print("\n完成！")


if __name__ == "__main__":
    main()
