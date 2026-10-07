import base64
import io
import json
import re
import time
import copy
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


# 기준자료의 외국법인 회사코드는 종목코드와 별도 체계이다.
# KIND 원문에서 종목코드를 확인한 대응만 사용한다.
FOREIGN_STOCK_CODES = {
    "USA11": "950140",  # 잉글우드랩
    "USA12": "950160",  # 코오롱티슈진
    "HKG21": "900290",  # GRT
}


def normalize_stock_code(value: Any) -> str:
    code = clean_text(value).upper()
    if code.endswith(".0") and code[:-2].isdigit():
        code = code[:-2]
    if len(code) == 7 and code.startswith("A"):
        code = code[1:]
    if code.isdigit() and len(code) <= 6:
        code = code.zfill(6)
    return code if re.fullmatch(r"[0-9]{6}|[0-9]{4}[A-Z][0-9]", code) else ""


def company_code_to_stock_code(value: Any) -> str:
    code = clean_text(value).upper()
    if code.endswith(".0") and code[:-2].isdigit():
        code = code[:-2]
    if code in FOREIGN_STOCK_CODES:
        return FOREIGN_STOCK_CODES[code]
    # 기존 로더가 회사코드를 여섯 자리로 패딩한 경우도 지원한다.
    if code.isdigit() and len(code) <= 6 and len(str(int(code))) <= 5:
        return str(int(code)).zfill(5) + "0"
    if re.fullmatch(r"[0-9]{4}[A-Z]", code):
        return code + "0"
    return ""


def reference_stock_codes(company: pd.DataFrame) -> set:
    targets = set()
    for row in company.to_dict("records"):
        explicit = clean_text(row.get("종목코드"))
        code = normalize_stock_code(explicit) if explicit else company_code_to_stock_code(row.get("회사코드"))
        if not code:
            raise ValueError("대상법인 기준자료에서 종목코드 대응을 확인할 수 없습니다: "
                             f"{clean_text(row.get('회사명'))}. 해당 행에 종목코드 열을 추가해주세요.")
        targets.add(code)
    return targets


def normalize_company_name(value: Any) -> str:
    text = clean_text(value)
    text = re.sub(r"\(주\)|㈜|주식회사", "", text)
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text).lower()




def empty_state() -> Dict[str, Any]:
    return {
        "cutoff": None,
        "rows": [],
        "latest_batch": [],
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














def read_shared_state(cfg: Dict[str, str]) -> Dict[str, Any]:
    response = requests.get(
        github_state_url(cfg), headers=github_headers(cfg),
        params={"ref": cfg["branch"]}, timeout=20,
    )
    if response.status_code == 404:
        # 저장 파일이 없을 때만 새 자료로 시작한다.
        repo_response = requests.get(
            f"https://api.github.com/repos/{quote(cfg['owner'])}/{quote(cfg['repo'])}",
            headers=github_headers(cfg), timeout=15,
        )
        repo_response.raise_for_status()
        branch_response = requests.get(
            f"https://api.github.com/repos/{quote(cfg['owner'])}/{quote(cfg['repo'])}/branches/{quote(cfg['branch'], safe='')}",
            headers=github_headers(cfg), timeout=15,
        )
        branch_response.raise_for_status()
        return empty_state()
    response.raise_for_status()
    payload = response.json()
    state = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("rows"), list):
        raise ValueError("공유 공시목록 파일의 형식이 올바르지 않습니다.")
    state["_sha"] = payload["sha"]
    state.pop("link_cache", None)
    for row in state["rows"]:
        for key in ("상세URL", "링크상태"):
            row.pop(key, None)
    for key, default in empty_state().items():
        state.setdefault(key, default)
    return state


def save_state(state: Dict[str, Any], cfg: Dict[str, str]) -> None:
    clean_state = {k: v for k, v in state.items() if not k.startswith("_")}
    body = {
        "message": f"Update disclosure state {now_kst().isoformat()}",
        "content": base64.b64encode(json.dumps(clean_state, ensure_ascii=False).encode("utf-8")).decode("ascii"),
        "branch": cfg["branch"],
    }
    if state.get("_sha"):
        body["sha"] = state["_sha"]
    response = requests.put(github_state_url(cfg), headers=github_headers(cfg), json=body, timeout=30)
    response.raise_for_status()
    state["_sha"] = response.json()["content"]["sha"]


def commit_upload(rows_df: pd.DataFrame, filename: str, cfg: Dict[str, str]):
    # 동시 저장 충돌 시 최신 목록을 다시 읽고 신규 행만 병합한다.
    for attempt in range(3):
        current = read_shared_state(cfg)
        working, status, count = merge_upload(copy.deepcopy(current), rows_df, filename)
        try:
            save_state(working, cfg)
            return working, status, count
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code not in (409, 422) or attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError("공유 저장을 완료하지 못했습니다. 다시 시도해주세요.")


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


def load_reference_data(market: str):
    company = read_reference_csv(reference_path(f"{market}_company.csv"))
    fmt = read_reference_csv(reference_path(f"{market}_format.csv"))
    if company.empty or "회사코드" not in company or "회사명" not in company:
        raise ValueError(f"{market}_company.csv의 회사코드·회사명 열을 확인해주세요.")
    if fmt.empty or "서식명" not in fmt:
        raise ValueError(f"{market}_format.csv의 서식명 열을 확인해주세요.")
    company["회사코드"] = company["회사코드"].map(normalize_code)
    reference_stock_codes(company)  # 알 수 없는 코드가 있으면 명시적으로 알린다.
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
                tables = pd.read_html(io.StringIO(html), converters={name: str for name in ALIASES["stock_code"] + ALIASES["company_code"] + ALIASES["acceptance_no"]})
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
        raw = raw.fillna("")
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

        acceptance_no = (
            clean_text(row.get(mapping["acceptance_no"], ""))
            if mapping["acceptance_no"]
            else ""
        )
        acceptance_no = re.sub(r"[^0-9]", "", acceptance_no)
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
                    normalize_stock_code(row.get(mapping["stock_code"], ""))
                    if mapping["stock_code"]
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

    existing = {row_key(row) for row in state.get("rows", [])}
    new_rows = []

    for row in incoming:
        key = row_key(row)
        if key in existing:
            continue
        existing.add(key)
        new_rows.append(row)

    state["rows"] = state.get("rows", []) + new_rows
    state["latest_batch"] = new_rows
    state["cutoff"] = max(filter(None, (previous_cutoff, incoming_max)), key=pd.to_datetime)
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
    target_stock_codes = reference_stock_codes(company)

    matched = []
    for row in rows:
        title = clean_text(row.get("공시제목"))
        raw_stock_code = clean_text(row.get("종목코드"))
        stock_code = normalize_stock_code(raw_stock_code) if raw_stock_code else company_code_to_stock_code(row.get("회사코드"))

        if title.startswith(("추가상장", "변경상장")):
            continue
        if "종속회사의 주요경영사항" in title:
            continue

        form_match = any(form in title for form in target_forms)
        # 법인명·제출인명은 판별에 사용하지 않는다.
        company_match = bool(stock_code) and stock_code in target_stock_codes
        if form_match and company_match:
            row = dict(row)
            row["종목코드"] = stock_code
            row["공시시각표시"] = fmt_dt(row.get("공시시각"))
            matched.append(row)

    result = pd.DataFrame(matched)
    return result.sort_values("공시시각") if not result.empty else result




RESULT_COLUMNS = ["공시시각표시", "종목코드", "회사명", "공시제목", "제출인"]


def show_result(result: pd.DataFrame, market_name: str):
    if result.empty:
        st.info(f"선택한 날짜의 누적 공시 중 {market_name} 영문공시 대상이 없습니다.")
        return
    st.dataframe(result[RESULT_COLUMNS], hide_index=True, use_container_width=True)


st.title("🎯 영문공시 대상 조회")
st.caption("KIND에서 다운로드한 공시목록을 종목코드로 판별합니다. 업로드한 목록은 모든 접속자가 함께 사용합니다.")
storage_cfg = github_config()
if not storage_cfg:
    st.error("공유 저장 설정이 필요합니다. Streamlit 앱의 Secrets에 [github] 설정을 입력해주세요.")
    st.stop()

try:
    df_kospi_format, df_kospi_company = load_reference_data("kospi")
    df_kosdaq_format, df_kosdaq_company = load_reference_data("kosdaq")
    state = read_shared_state(storage_cfg)
except Exception:
    st.error("공유 공시목록 또는 기준자료를 불러오지 못했습니다. GitHub 설정·접근권한과 기준 CSV를 확인한 후 새로고침해주세요.")
    st.stop()

with st.sidebar:
    selected_date = st.date_input("조회 날짜", value=now_kst().date())
    show_all_dates = st.checkbox("모든 날짜의 누적 결과 보기", value=False)
    st.success("공시목록 공유 저장 연결됨")
    st.caption("다른 접속자의 업데이트를 30초마다 확인합니다. 조회 날짜는 각자 선택할 수 있습니다.")

st.subheader("1. KIND 공시목록 업로드")
uploaded_disclosures = st.file_uploader("KIND에서 다운로드한 공시목록 엑셀을 올려주세요.", type=["xlsx", "xls"], key="disclosure_upload")
if st.button("업로드한 목록 반영", disabled=uploaded_disclosures is None):
    try:
        raw_df, sheet_name, header_row = read_excel_safely(uploaded_disclosures)
        normalized_df = normalize_uploaded_rows(raw_df, uploaded_disclosures, sheet_name, header_row)
        state, status, new_count = commit_upload(normalized_df, uploaded_disclosures.name, storage_cfg)
        st.success(f"신규 공시 {new_count}건을 공유 목록에 저장했습니다.") if new_count else st.info("새로 추가된 공시가 없습니다. 기존 공유 목록을 유지합니다.")
    except Exception:
        st.error("공유 목록에 저장하지 못했습니다. 저장 권한·연결 상태를 확인한 후 다시 반영해주세요.")

seed_path = ROOT / "data" / "initial_disclosures.json"
if seed_path.exists():
    seed_rows = json.loads(seed_path.read_text(encoding="utf-8"))["rows"]
    saved_keys = {row_key(row) for row in state["rows"]}
    missing_seed_rows = [row for row in seed_rows if row_key(row) not in saved_keys]
    if missing_seed_rows:
        st.info("이전에 전달한 10월 1·2·6·7일 공시목록 중 아직 공유 저장되지 않은 자료를 불러올 수 있습니다.")
        if st.button("이전에 올린 공시목록 불러오기"):
            try:
                state, _, count = commit_upload(pd.DataFrame(seed_rows), "기존 업로드 자료 복원", storage_cfg)
                st.success(f"기존 자료 중 {count}건을 공유 목록에 추가했습니다.")
            except Exception:
                st.error("기존 자료를 공유 저장하지 못했습니다. 연결·저장 권한을 확인한 후 다시 시도해주세요.")


@st.fragment(run_every="30s")
def shared_results():
    st.subheader("2. 누적 결과")
    st.button("공유 목록 새로고침", key="refresh_shared")
    try:
        current = read_shared_state(storage_cfg)
    except Exception:
        st.error("공유 목록을 갱신하지 못했습니다. 연결을 확인한 후 새로고침해주세요.")
        return
    col1, col2, col3 = st.columns(3)
    col1.metric("최신 공시 시각", fmt_dt(current.get("cutoff")))
    col2.metric("최근 업로드 시각", fmt_dt(current.get("last_upload")))
    col3.metric("누적 공시 수", f"{len(current['rows']):,}건")
    visible = current["rows"]
    if not show_all_dates:
        visible = [r for r in visible if pd.to_datetime(r["공시시각"]).tz_convert(KST).date() == selected_date]
    for market, name, fmt, company in (
        ("kospi", "코스피", df_kospi_format, df_kospi_company),
        ("kosdaq", "코스닥", df_kosdaq_format, df_kosdaq_company),
    ):
        st.subheader(f"{name} 영문공시 대상")
        result = filter_market(visible, fmt, company)
        show_result(result, name)
        if not result.empty:
            st.download_button(f"{name} 결과 CSV 다운로드", result[RESULT_COLUMNS].to_csv(index=False).encode("utf-8-sig"),
                file_name=f"{market}_{'all' if show_all_dates else selected_date}.csv", mime="text/csv", key=f"download_{market}")
    with st.expander("업로드 이력"):
        if current.get("upload_history"):
            st.dataframe(pd.DataFrame(current["upload_history"]).rename(columns={
                "file_name": "파일명", "uploaded_at": "업로드 시각", "file_latest_time": "파일 내 최신 공시",
                "new_rows": "신규 공시 수", "status": "처리 결과",
            }), hide_index=True, use_container_width=True)
        else:
            st.info("아직 업로드 이력이 없습니다.")


shared_results()
