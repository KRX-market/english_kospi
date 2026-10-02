# 영문공시 대상 조회

KIND를 자동으로 크롤링하지 않고, 이용자가 KIND 오늘의공시에서 직접 다운로드한 공시목록 엑셀을 업로드하면 영문공시 대상을 판별하는 Streamlit 앱입니다.

## 기준자료

GitHub 저장소의 최상위 또는 config 폴더에 다음 파일을 넣으면 자동으로 사용합니다.

- config/kospi_company.csv
- config/kospi_format.csv
- config/kosdaq_company.csv
- config/kosdaq_format.csv

법인 파일에는 회사코드 열이, 서식 파일에는 서식명 열이 필요합니다.

현재 기존 폴더에 있는 `eng.py`, `eng_keep.py`, `kind_state.sqlite3`, `nohup.out`, 과거 공시 엑셀 파일은 새 업로드 방식의 앱 실행에 필요하지 않습니다. GitHub 저장소에는 새 `app.py`, `requirements.txt`, 기준 CSV 4개만 우선 올리면 됩니다.

## 동작 방식

1. KIND에서 다운로드한 xlsx 또는 xls 공시목록을 업로드합니다.
2. 파일 내 가장 최근 공시 시각을 계산합니다.
3. 기존 기준시각 이후의 공시만 신규 데이터로 누적합니다.
4. 기존 기준시각보다 과거인 파일이면 업데이트된 공시가 없습니다를 표시합니다.
5. 코스피·코스닥 버튼을 누르면 대상서식과 대상법인 기준으로 필터링합니다.
6. 공시 URL은 엑셀 URL 열, 셀 하이퍼링크, 접수번호 순으로 확인해 클릭 링크를 만듭니다.

KIND의 `오늘의공시.xls`는 실제로 CP949 HTML 표인 경우가 있어 이를 지원합니다. 이 형식은 `시간`과 `접수일자`를 합쳐 공시시각을 만들고, `종목코드`만 제공하므로 대상법인 CSV의 회사명으로 대조합니다. 파일에 상세URL·하이퍼링크·접수번호가 없으면 개별 공시의 직접 KIND 상세 URL은 복원할 수 없습니다.

## GitHub 누적 저장소 설정

Streamlit Secrets에 다음을 설정하면 누적 상태를 private GitHub repository의 JSON 파일에 저장할 수 있습니다.

```toml
[github]
token = "github_pat_..."
owner = "github-owner"
repo = "private-repository"
branch = "main"
path = "data/disclosure_state.json"

[openai]
api_key = "sk-..."
model = "gpt-4.1-mini"
```

GitHub 설정이 없으면 현재 브라우저 세션에만 저장됩니다. `openai` 설정을 추가하면 필터링 결과의 회사명·공시제목·공시일자를 검색어로 사용해 KIND 상세 링크를 찾습니다. 검색 결과가 회사·제목·일자와 정확히 일치하지 않으면 링크를 기록하지 않습니다. 운영용에서는 private repository와 최소권한 토큰을 사용해야 합니다.
