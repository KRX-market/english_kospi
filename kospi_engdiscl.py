import base64
import hashlib
import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st


KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent

st.set_page_config(page_title="영문공시 대상 조회", page_icon="🎯", layout="wide")


def now_kst() -> datetime:
    return datetime.now(KST).replace(microsecond=0)


def clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def fmt_dt(value: Optional[str]) -> str:
    if not value:
        return "-"
    try:
        dt = pd.to_datetime(value)
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.tz_localize(KST)
        else:
            dt = dt.tz_convert(KST)
        return dt.strftime("%Y.%m.%d %H:%M")
    except Exception:
        return str(value)


def iso_dt(value: Any) -> Optional[str]:
    text = clean_text(value)
    if not text:
        return None
    try:
        dt = pd.to_datetime(text)
        if pd.isna(dt):
            return None
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.tz_localize(KST)
        else:
            dt = dt.tz_convert(KST)
        return dt.isoformat()
    except Exception:
        return None


def normalize_code(value: Any) -> str:
    text = clean_text(value)
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text


def normalize_company_name(value: Any) -> str:
    text = clean_text(value)
    text = re.sub(r"\(주\)|㈜|주식회사", "", text)
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text).lower()


def file_hash(uploaded_file) -> str:
    return hashlib.sha256(uploaded_file.getvalue()).hexdigest()


def empty_state() -> Dict[str, Any]:
    return {
        "cutoff": None,
        "rows": [],
        "latest_batch": [],
        "link_cache": {},
        "last_upload": None,
        "upload_history": [],
    }


# ---------------------------------------------------------------------
# 누적 상태 저장
# ---------------------------------------------------------------------
def github_config() -> Optional[Dict[str, str]]:
    try:
        cfg = st.secrets["github"]
        if not all(str(cfg.get(k, "")).strip() for k in ("token", "owner", "repo")):
            return None
        return {
            "token": str(cfg["token"]),
            "owner": str(cfg["owner"]),
            "repo": str(cfg["repo"]),
            "branch": str(cfg.get("branch", "main")),
            "path": str(cfg.get("path", "data/disclosure_state.json")),
        }
    except Exception:
        return None


def github_headers(cfg: Dict[str, str]) -> Dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {cfg['token']}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_state_url(cfg: Dict[str, str]) -> str:
    return (
        f"https://api.github.com/repos/{quote(cfg['owner'])}/"
        f"{quote(cfg['repo'])}/contents/{cfg['path']}"
    )


def openai_config() -> Optional[Dict[str, str]]:
    """AI 링크 검색용 API 설정. API 키는 저장소에 넣지 않고 Secrets에서 읽는다."""
    try:
        cfg = st.secrets["openai"]
        api_key = str(cfg.get("api_key", "")).strip()
        if not api_key:
            return None
        return {
            "api_key": api_key,
            "model": str(cfg.get("model", "gpt-4.1-mini")),
        }
    except Exception:
        return None


def _walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def find_kind_url_with_ai(row: Dict[str, Any], cfg: Dict[str, str]) -> Tuple[str, str]:
    """공시 메타데이터로 검색하고, 정확히 확인된 KIND 상세 URL만 반환한다."""
    dt = pd.to_datetime(row.get("공시시각"))
    expected_date = dt.strftime("%Y-%m-%d")
    expected_time = dt.strftime("%H:%M")
    company = clean_text(row.get("회사명"))
    title = clean_text(row.get("공시제목"))
    stock_code = clean_text(row.get("종목코드"))

    query = (
        f'site:kind.krx.co.kr/common/disclsviewer.do "{company}" '
        f'"{title}" "{expected_date}"'
    )
    prompt = f"""
Find the exact KIND disclosure detail page for this Korean exchange disclosure.
Search only public pages on kind.krx.co.kr.

Company: {company}
Stock code: {stock_code}
Disclosure title: {title}
Disclosure date: {expected_date}
Disclosure time: {expected_time}

The result must match the company, title, and disclosure date. Do not return a
different filing with a similar title. Do not invent or construct a URL.
Copy the URL exactly from a web-search result. If an exact match is not found,
return NO_MATCH.

Return exactly these lines:
STATUS: EXACT or NO_MATCH
MATCH_DATE: YYYY-MM-DD or NONE
URL: https://... or NONE
""".strip()

    payload = {
        "model": cfg["model"],
        "tools": [
            {
                "type": "web_search",
                "filters": {"allowed_domains": ["kind.krx.co.kr"]},
            }
        ],
        "tool_choice": "required",
        "input": f"Search query: {query}\n\n{prompt}",
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=45,
        )
        response.raise_for_status()
        result = response.json()
    except Exception as exc:
        return "", f"검색 오류: {exc}"

    texts = []
    candidates = []
    for node in _walk_json(result):
        if isinstance(node.get("text"), str):
            texts.append(node["text"])
        if node.get("type") == "url_citation" and node.get("url"):
            candidates.append(node["url"])

    response_text = "\n".join(texts)
    candidates.extend(
        re.findall(r"https://kind\.krx\.co\.kr/[^\s<>\"']+", response_text)
    )
    candidates = list(dict.fromkeys(url.rstrip(".,;)") for url in candidates))
    candidates = [
        url for url in candidates
        if "kind.krx.co.kr" in url
        and "/common/disclsviewer.do" in url
    ]

    status_match = re.search(r"STATUS\s*:\s*(EXACT|NO_MATCH)", response_text, re.I)
    date_match = re.search(r"MATCH_DATE\s*:\s*(\d{4}-\d{2}-\d{2})", response_text, re.I)
    url_match = re.search(r"URL\s*:\s*(https://kind\.krx\.co\.kr/[^\s]+)", response_text, re.I)

    if not status_match or status_match.group(1).upper() != "EXACT":
        return "", "정확한 링크 없음"
    if not date_match or date_match.group(1) != expected_date:
        return "", "공시일자 불일치"

    url = url_match.group(1).rstrip(".,;)") if url_match else ""
    if url not in candidates:
        url = candidates[0] if len(candidates) == 1 else ""
    if not url:
        return "", "검색 결과 URL 확인 불가"

    # AI의 서술 결과에도 기본 식별정보가 포함됐는지 추가 확인한다.
    if company not in response_text or expected_date not in response_text:
        return "", "검색 결과 식별정보 불일치"
    return url, "확인됨"


def load_state() -> Tuple[Dict[str, Any], Optional[Dict[str, str]]]:
    cfg = github_config()
    if not cfg:
        return st.session_state.setdefault("local_state", empty_state()), None

    try:
        response = requests.get(
            github_state_url(cfg),
            headers=github_headers(cfg),
            params={"ref": cfg["branch"]},
            timeout=15,
        )
        if response.status_code == 404:
            return empty_state(), cfg
        response.raise_for_status()
        payload = response.json()
        content = base64.b64decode(payload["content"]).decode("utf-8")
        state = json.loads(content)
        state["_sha"] = payload.get("sha")
        return state, cfg
    except Exception as exc:
        st.warning(f"누적 저장소를 불러오지 못해 현재 세션 기준으로 동작합니다: {exc}")
        return st.session_state.setdefault("local_state", empty_state()), None


def save_state(state: Dict[str, Any], cfg: Optional[Dict[str, str]]) -> None:
    clean_state = {k: v for k, v in state.items() if not k.startswith("_")}
    if not cfg:
        st.session_state["local_state"] = clean_state
        return

    response = requests.get(
        github_state_url(cfg),
        headers=github_headers(cfg),
        params={"ref": cfg["branch"]},
        timeout=15,
    )
    sha = response.json().get("sha") if response.status_code == 200 else None
    content = base64.b64encode(
        json.dumps(clean_state, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("ascii")
    body = {
        "message": f"Update disclosure state {now_kst().isoformat()}",
        "content": content,
        "branch": cfg["branch"],
    }
    if sha:
        body["sha"] = sha

    response = requests.put(
        github_state_url(cfg),
        headers=github_headers(cfg),
        json=body,
        timeout=20,
    )
    response.raise_for_status()


# ---------------------------------------------------------------------
# 기준자료
# ---------------------------------------------------------------------
def read_reference_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return read_csv_flexible(path)


def reference_path(filename: str) -> Path:
    """현재 서버처럼 루트에 CSV가 있는 경우와 config 폴더를 모두 지원한다."""
    root_path = ROOT / filename
    config_path = ROOT / "config" / filename
    return root_path if root_path.exists() else config_path


def read_csv_flexible(source) -> pd.DataFrame:
    """KIND/사내 엑셀 저장 CSV에서 흔한 인코딩을 순서대로 시도한다."""
    for encoding in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            if hasattr(source, "seek"):
                source.seek(0)
            return pd.read_csv(source, dtype=str, encoding=encoding).fillna("")
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
        except Exception:
            continue
    return pd.DataFrame()


def load_reference_data(uploaded_company, uploaded_format, market: str):
    company_path = reference_path(f"{market}_company.csv")
    format_path = reference_path(f"{market}_format.csv")
    company = (
        read_csv_flexible(uploaded_company)
        if uploaded_company is not None
        else read_reference_csv(company_path)
    )
    fmt = (
        read_csv_flexible(uploaded_format)
        if uploaded_format is not None
        else read_reference_csv(format_path)
    )
    if "회사코드" in company.columns:
        company["회사코드"] = company["회사코드"].map(normalize_code)
    return fmt, company


# ---------------------------------------------------------------------
# KIND 엑셀 파싱
# ---------------------------------------------------------------------
ALIASES = {
    "time": ["시간", "공시시간", "접수시간", "제출일시", "접수일시", "공시일시", "일시"],
    "date": ["접수일자", "공시일자", "제출일자", "공시날짜", "날짜", "일자"],
    "company_code": ["회사코드", "발행기관코드", "법인코드"],
    "stock_code": ["종목코드", "종목번호"],
    "company_name": ["회사명", "법인명", "종목명", "회사"],
    "title": ["공시제목", "공시 제목", "공시명", "제목", "서식명"],
    "submitter": ["제출인", "제출자", "제출법인"],
    "url": ["상세URL", "공시URL", "URL", "상세보기", "공시보기", "링크"],
    "acceptance_no": ["접수번호", "접수 번호", "접수No"],
}


def canonical(value: Any) -> str:
    return re.sub(r"[\s_\-()]+", "", clean_text(value)).lower()


def find_header_row(raw: pd.DataFrame) -> int:
    aliases = {canonical(x) for values in ALIASES.values() for x in values}
    best_row, best_score = 0, -1
    for idx in range(min(len(raw), 12)):
        score = len({canonical(x) for x in raw.iloc[idx].tolist()} & aliases)
        if score > best_score:
            best_row, best_score = idx, score
    return best_row


def read_excel_safely(uploaded_file):
    data = uploaded_file.getvalue()

    # KIND의 '오늘의공시.xls'는 확장자만 xls이고 실제 내용은
    # CP949 HTML table인 경우가 있다. 이 형식을 먼저 처리한다.
    lowered = data.lstrip().lower()
    if lowered.startswith(b"<!doctype html") or lowered.startswith(b"<html") or b"<table" in lowered[:2000]:
        for encoding in ("cp949", "euc-kr", "utf-8"):
            try:
                html = data.decode(encoding)
                tables = pd.read_html(io.StringIO(html))
                if tables:
                    aliases = {canonical(x) for values in ALIASES.values() for x in values}
                    best = max(
                        tables,
                        key=lambda table: len(
                            {canonical(x) for x in table.columns} & aliases
                        ),
                    )
                    return best.fillna(""), "__kind_html_xls__", 0
            except (UnicodeDecodeError, ValueError, ImportError):
                continue
        raise ValueError("KIND HTML 형식 XLS 파일을 읽지 못했습니다.")

    excel = pd.ExcelFile(io.BytesIO(data))
    best = None

    for sheet in excel.sheet_names:
        raw = pd.read_excel(io.BytesIO(data), sheet_name=sheet, header=None, dtype=str)
        raw = raw.dropna(how="all")
        if raw.empty:
            continue
        header_row = find_header_row(raw)
        aliases = {canonical(x) for values in ALIASES.values() for x in values}
        score = len({canonical(x) for x in raw.iloc[header_row].tolist()} & aliases)
        candidate = pd.read_excel(
            io.BytesIO(data), sheet_name=sheet, header=header_row, dtype=str
        ).fillna("")
        if best is None or score > best[3]:
            best = (candidate, sheet, header_row, score)

    if best is None:
        raise ValueError("엑셀에서 읽을 수 있는 시트를 찾지 못했습니다.")
    return best[0], best[1], best[2]


def choose_column(columns: Iterable[str], candidates: List[str]) -> Optional[str]:
    normalized = {canonical(c): c for c in columns}
    for candidate in candidates:
        if canonical(candidate) in normalized:
            return normalized[canonical(candidate)]
    return None


def extract_formula_url(value: Any) -> str:
    match = re.search(r"https?://[^\"')]+", clean_text(value))
    return match.group(0) if match else ""


def hyperlink_map(uploaded_file, sheet_name: str, header_row: int):
    if not uploaded_file.name.lower().endswith(".xlsx"):
        return {}
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(uploaded_file.getvalue()), data_only=False)
        sheet = workbook[sheet_name]
        links = {}
        for row in sheet.iter_rows():
            for cell in row:
                if cell.hyperlink and cell.hyperlink.target:
                    links[(cell.row - header_row - 1, cell.column - 1)] = cell.hyperlink.target
        return links
    except Exception:
        return {}


def normalize_uploaded_rows(df: pd.DataFrame, uploaded_file, sheet_name: str, header_row: int):
    columns = list(df.columns)
    mapping = {
        key: choose_column(columns, candidates)
        for key, candidates in ALIASES.items()
    }

    if not mapping["title"]:
        raise ValueError("공시 제목 열을 찾지 못했습니다.")
    if not mapping["time"]:
        raise ValueError("공시 시각 열을 찾지 못했습니다.")
    if not mapping["company_code"] and not mapping["stock_code"] and not mapping["company_name"]:
        raise ValueError("회사코드·종목코드 또는 회사명 열을 찾지 못했습니다.")

    links = hyperlink_map(uploaded_file, sheet_name, header_row)
    date_column = mapping["date"]
    result = []

    for row_idx, row in df.iterrows():
        title = clean_text(row.get(mapping["title"], ""))
        raw_time = clean_text(row.get(mapping["time"], ""))
        if date_column and date_column != mapping["time"]:
            raw_time = f"{clean_text(row.get(date_column, ''))} {raw_time}".strip()
        disclosure_time = iso_dt(raw_time)
        if not title or not disclosure_time:
            continue

        url = clean_text(row.get(mapping["url"], "")) if mapping["url"] else ""
        url = extract_formula_url(url) or url
        if not url:
            for (excel_row, _), target in links.items():
                if excel_row == row_idx:
                    url = target
                    break

        acceptance_no = (
            clean_text(row.get(mapping["acceptance_no"], ""))
            if mapping["acceptance_no"]
            else ""
        )
        acceptance_no = re.sub(r"[^0-9]", "", acceptance_no)
        if not url and acceptance_no:
            url = (
                "https://kind.krx.co.kr/common/disclsviewer.do"
                f"?method=search&acptno={acceptance_no}"
            )

        source_code_column = mapping["company_code"] or mapping["stock_code"]
        source_code_type = (
            "회사코드" if mapping["company_code"] else
            "종목코드" if mapping["stock_code"] else
            ""
        )

        result.append(
            {
                "공시시각": disclosure_time,
                "회사코드": (
                    normalize_code(row.get(source_code_column, ""))
                    if source_code_type == "회사코드"
                    else ""
                ),
                "종목코드": (
                    normalize_code(row.get(source_code_column, ""))
                    if source_code_type == "종목코드"
                    else ""
                ),
                "코드종류": source_code_type,
                "회사명": (
                    clean_text(row.get(mapping["company_name"], ""))
                    if mapping["company_name"]
                    else ""
                ),
                "공시제목": title,
                "제출인": (
                    clean_text(row.get(mapping["submitter"], ""))
                    if mapping["submitter"]
                    else ""
                ),
                "상세URL": url,
                "접수번호": acceptance_no,
            }
        )

    if not result:
        raise ValueError("공시 시각과 제목을 모두 가진 행을 찾지 못했습니다.")
    return pd.DataFrame(result)


# ---------------------------------------------------------------------
# 누적 처리
# ---------------------------------------------------------------------
def row_key(row: Dict[str, Any]) -> str:
    if row.get("접수번호"):
        return f"acpt:{row['접수번호']}"
    return "|".join(
        str(row.get(k, ""))
        for k in (
            "공시시각",
            "회사코드",
            "종목코드",
            "회사명",
            "공시제목",
            "제출인",
        )
    )


def merge_upload(state: Dict[str, Any], rows_df: pd.DataFrame, filename: str):
    upload_time = now_kst().isoformat()
    incoming = rows_df.to_dict("records")
    incoming_max = max(row["공시시각"] for row in incoming)
    previous_cutoff = state.get("cutoff")

    history_item = {
        "file_name": filename,
        "uploaded_at": upload_time,
        "file_latest_time": incoming_max,
        "new_rows": 0,
        "status": "no_update",
    }

    if previous_cutoff and pd.to_datetime(incoming_max) <= pd.to_datetime(previous_cutoff):
        state["latest_batch"] = []
        state["last_upload"] = upload_time
        state.setdefault("upload_history", []).insert(0, history_item)
        state["upload_history"] = state["upload_history"][:20]
        return state, "no_update", 0

    existing = {row_key(row) for row in state.get("rows", [])}
    new_rows = []

    for row in incoming:
        if previous_cutoff and pd.to_datetime(row["공시시각"]) <= pd.to_datetime(previous_cutoff):
            continue
        key = row_key(row)
        if key in existing:
            continue
        existing.add(key)
        new_rows.append(row)

    state["rows"] = state.get("rows", []) + new_rows
    state["latest_batch"] = new_rows
    state["cutoff"] = max(previous_cutoff, incoming_max) if previous_cutoff else incoming_max
    state["last_upload"] = upload_time
    history_item["new_rows"] = len(new_rows)
    history_item["status"] = "updated" if new_rows else "no_update"
    state.setdefault("upload_history", []).insert(0, history_item)
    state["upload_history"] = state["upload_history"][:20]
    return state, history_item["status"], len(new_rows)


# ---------------------------------------------------------------------
# 필터링
# ---------------------------------------------------------------------
def filter_market(rows: List[Dict[str, Any]], fmt: pd.DataFrame, company: pd.DataFrame):
    if not rows or fmt.empty or company.empty:
        return pd.DataFrame()

    form_col = "서식명" if "서식명" in fmt.columns else fmt.columns[0]
    target_forms = [clean_text(x) for x in fmt[form_col].tolist() if clean_text(x)]
    target_codes = set(company["회사코드"].map(normalize_code)) if "회사코드" in company else set()
    target_names = (
        set(company["회사명"].map(normalize_company_name))
        if "회사명" in company
        else set()
    )

    matched = []
    for row in rows:
        title = clean_text(row.get("공시제목"))
        code = normalize_code(row.get("회사코드"))
        name = clean_text(row.get("회사명"))

        if title.startswith(("추가상장", "변경상장")):
            continue
        if "종속회사의 주요경영사항" in title:
            continue

        form_match = any(form in title for form in target_forms)
        # KIND 오늘의공시 XLS는 '종목코드'만 제공하므로 기준 CSV의
        # '회사코드'와 직접 비교하지 않고 회사명으로 대조한다.
        if row.get("코드종류") == "회사코드" and code:
            company_match = code in target_codes
        else:
            company_match = normalize_company_name(name) in target_names
        if form_match and company_match:
            row = dict(row)
            row["공시시각표시"] = fmt_dt(row.get("공시시각"))
            matched.append(row)

    result = pd.DataFrame(matched)
    return result.sort_values("공시시각") if not result.empty else result


def add_ai_links(state: Dict[str, Any], result: pd.DataFrame, cfg: Optional[Dict[str, str]]):
    """필터링 결과 중 URL이 없는 행만 AI로 검색하고 누적 상태에도 반영한다."""
    if result.empty or not cfg:
        return result, False

    cache = state.setdefault("link_cache", {})
    updates = {}
    progress = st.progress(0, text="KIND 상세 링크를 AI로 확인하는 중...")
    changed = False

    for index, row in result.iterrows():
        row_dict = row.to_dict()
        key = row_key(row_dict)
        cached = cache.get(key)

        if row_dict.get("상세URL"):
            url, status = row_dict["상세URL"], "엑셀 제공 URL"
        elif cached is not None:
            url, status = cached.get("url", ""), cached.get("status", "")
        else:
            url, status = find_kind_url_with_ai(row_dict, cfg)
            cache[key] = {
                "url": url,
                "status": status,
                "checked_at": now_kst().isoformat(),
            }
            changed = True

        result.at[index, "상세URL"] = url
        result.at[index, "링크상태"] = status
        updates[key] = {"상세URL": url, "링크상태": status}
        progress.progress(
            int((len(updates) / len(result)) * 100),
            text=f"KIND 상세 링크 확인 중... {len(updates)}/{len(result)}건",
        )

    progress.empty()

    # 현재 결과뿐 아니라 누적 state에도 찾은 URL을 기록한다.
    for collection_name in ("rows", "latest_batch"):
        for stored_row in state.get(collection_name, []):
            update = updates.get(row_key(stored_row))
            if update:
                stored_row.update(update)

    return result, changed


def show_result(result: pd.DataFrame, market_name: str):
    if result.empty:
        st.info(f"현재 업데이트분 중 {market_name} 영문공시 대상이 없습니다.")
        return

    if "링크상태" not in result.columns:
        result["링크상태"] = result["상세URL"].map(
            lambda value: "엑셀 제공 URL" if value else "링크 미확인"
        )
    columns = ["공시시각표시", "회사명", "공시제목", "제출인", "상세URL", "링크상태"]
    st.dataframe(
        result[columns],
        column_config={"상세URL": st.column_config.LinkColumn("공시보기")},
        hide_index=True,
        use_container_width=True,
    )


# ---------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------
st.title("🎯 영문공시 대상 조회")
st.caption("KIND를 자동 조회하지 않습니다. KIND에서 직접 다운로드한 공시목록을 업로드해 판정합니다.")

state, storage_cfg = load_state()

with st.sidebar:
    st.header("기준자료")
    st.caption("저장소의 기준자료를 사용하거나, 이번 세션에서 CSV를 직접 올릴 수 있습니다.")
    uploaded_kospi_company = st.file_uploader("KOSPI 대상법인 CSV", type=["csv"], key="kospi_company")
    uploaded_kospi_format = st.file_uploader("KOSPI 대상서식 CSV", type=["csv"], key="kospi_format")
    uploaded_kosdaq_company = st.file_uploader("KOSDAQ 대상법인 CSV", type=["csv"], key="kosdaq_company")
    uploaded_kosdaq_format = st.file_uploader("KOSDAQ 대상서식 CSV", type=["csv"], key="kosdaq_format")
    if storage_cfg:
        st.success("누적 저장: GitHub 연결")
    else:
        st.info("누적 저장: 현재 브라우저 세션")

st.subheader("1. KIND 공시 제출 목록 업로드")
uploaded_disclosures = st.file_uploader(
    "KIND 오늘의공시에서 다운로드한 공시목록 엑셀을 올려주세요.",
    type=["xlsx", "xls"],
    key="disclosure_upload",
)

if uploaded_disclosures is not None:
    current_hash = file_hash(uploaded_disclosures)
    if st.session_state.get("processed_upload_hash") != current_hash:
        try:
            raw_df, sheet_name, header_row = read_excel_safely(uploaded_disclosures)
            normalized_df = normalize_uploaded_rows(
                raw_df, uploaded_disclosures, sheet_name, header_row
            )
            state, status, new_count = merge_upload(
                state, normalized_df, uploaded_disclosures.name
            )
            save_state(state, storage_cfg)
            st.session_state["processed_upload_hash"] = current_hash
            st.session_state["last_upload_message"] = (status, new_count)
            st.session_state["last_upload_error"] = None
        except Exception as exc:
            st.session_state["last_upload_error"] = str(exc)

    if st.session_state.get("last_upload_error"):
        st.error(f"업로드 파일을 처리하지 못했습니다: {st.session_state['last_upload_error']}")
    else:
        status, new_count = st.session_state.get("last_upload_message", (None, 0))
        if status == "updated":
            st.success(f"공시목록이 업데이트되었습니다. 신규 공시 {new_count}건")
        elif status == "no_update":
            st.info("업데이트된 공시가 없습니다.")

st.subheader("2. 현재 누적 상태")
metric1, metric2, metric3 = st.columns(3)
metric1.metric("기준시각", fmt_dt(state.get("cutoff")))
metric2.metric("최근 업로드 시각", fmt_dt(state.get("last_upload")))
metric3.metric("누적 공시 수", f"{len(state.get('rows', [])):,}건")

if state.get("last_upload") and state.get("upload_history"):
    latest_history = state["upload_history"][0]
    st.caption(
        f"최근 파일: {latest_history.get('file_name', '-')} | "
        f"파일 내 최신 공시: {fmt_dt(latest_history.get('file_latest_time'))}"
    )

st.divider()
st.subheader("3. 영문공시 대상 조회")
st.caption("새로 누적된 공시 중 대상서식·대상법인 조건을 만족하는 공시만 표시합니다.")
ai_cfg = openai_config()
use_ai_links = st.checkbox("AI로 KIND 상세 링크 찾기", value=True)
if use_ai_links and not ai_cfg:
    st.info("AI 링크 검색을 사용하려면 Streamlit Secrets에 OpenAI API 설정이 필요합니다.")

df_kospi_format, df_kospi_company = load_reference_data(
    uploaded_kospi_company, uploaded_kospi_format, "kospi"
)
df_kosdaq_format, df_kosdaq_company = load_reference_data(
    uploaded_kosdaq_company, uploaded_kosdaq_format, "kosdaq"
)

col1, col2 = st.columns(2)
with col1:
    if st.button("🚀 코스피 영문공시 대상 조회", use_container_width=True):
        result = filter_market(
            state.get("latest_batch", []), df_kospi_format, df_kospi_company
        )
        if use_ai_links and ai_cfg and not result.empty:
            result, changed = add_ai_links(state, result, ai_cfg)
            if changed:
                save_state(state, storage_cfg)
        show_result(result, "코스피")

with col2:
    if st.button("🚀 코스닥 영문공시 대상 조회", use_container_width=True):
        result = filter_market(
            state.get("latest_batch", []), df_kosdaq_format, df_kosdaq_company
        )
        if use_ai_links and ai_cfg and not result.empty:
            result, changed = add_ai_links(state, result, ai_cfg)
            if changed:
                save_state(state, storage_cfg)
        show_result(result, "코스닥")

with st.expander("업로드 이력 보기"):
    history = state.get("upload_history", [])
    if history:
        history_df = pd.DataFrame(history).rename(
            columns={
                "file_name": "파일명",
                "uploaded_at": "업로드 시각",
                "file_latest_time": "파일 내 최신 공시",
                "new_rows": "신규 공시 수",
                "status": "처리 결과",
            }
        )
        history_df["업로드 시각"] = history_df["업로드 시각"].map(fmt_dt)
        history_df["파일 내 최신 공시"] = history_df["파일 내 최신 공시"].map(fmt_dt)
        st.dataframe(history_df, hide_index=True, use_container_width=True)
    else:
        st.info("아직 업로드 이력이 없습니다.")
