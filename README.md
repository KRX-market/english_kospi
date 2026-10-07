# 영문공시 대상 조회 수정 안내

## 이번 변경

- 대상법인을 종목코드로 판별합니다. HDC, HS효성첨단소재, GKL, KB금융의 회사명 표기 차이로 인한 누락을 방지합니다.
- URL 검색, AI 호출, 재검색 버튼, URL 표시·다운로드 항목을 삭제했습니다. OpenAI API 키가 필요하지 않습니다.
- IP·PC·브라우저가 달라도 같은 GitHub 누적 파일을 조회합니다. 조회 날짜는 각자 선택합니다.
- 다른 사람이 업로드하면 열려 있는 화면에서 30초마다 업데이트를 확인합니다. '공유 목록 새로고침'으로 바로 확인할 수도 있습니다.
- 공유 설정이 없거나 조회에 실패하면 오류를 표시합니다. 개인 세션 저장으로 전환하지 않습니다.
- 동시 저장 충돌 시 최신 목록을 읽고 신규 행을 병합해 최대 3회 시도합니다. 실패하면 오류를 표시합니다.
- 기존에 전달한 XLS에서 추출한 10월 1·2·6·7일 목록을 복원 파일에 넣었습니다.

## 1. GitHub 파일 교체

1. 압축을 풀어 kind_updated 폴더를 엽니다.
2. 기존 GitHub 저장소에서 Add file → Upload files를 누릅니다.
3. kospi_engdiscl.py, requirements.txt, README.md를 올려 교체합니다. 실행 파일명이 다르면 현재 실행 파일명으로 코드 내용을 교체합니다.
4. data 폴더도 끌어 넣습니다. GitHub에서 data/initial_disclosures.json으로 올라갔는지 확인합니다.
5. Commit changes를 누릅니다. 기준 CSV 4개는 기존 파일을 유지합니다.

기존 data/disclosure_state.json은 지우거나 교체하지 마세요. 현재 누적 목록을 저장한 파일입니다. 이번 압축에는 그 파일을 넣지 않았습니다.

## 2. Streamlit 공유 저장 설정

Streamlit 앱 관리 → Settings → Secrets를 엽니다. 기존 [github] 설정이 있으면 owner, repo, branch, path를 그대로 유지하세요. 다른 저장소나 path로 바꾸면 별도 목록을 보게 됩니다.

```toml
[github]
token = "실제 GitHub 토큰"
owner = "누적 파일을 저장할 GitHub 계정명"
repo = "누적 파일을 저장할 저장소명"
branch = "main"
path = "data/disclosure_state.json"
```

처음 설정한다면 GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens에서 토큰을 생성합니다. Resource owner와 Repository access에서 누적 파일을 저장할 저장소를 선택하고 Repository permissions의 Contents를 Read and write로 설정합니다. 생성한 토큰을 위 token 값에 넣습니다. 실제 토큰을 GitHub 코드에 넣거나 다른 사람에게 보내지 마세요.

기존 [openai] 설정은 삭제해도 됩니다. secrets.toml.example은 양식이므로 실제 비밀키 파일을 GitHub에 올리지 않습니다.

Save 후 앱을 다시 실행합니다. 자동 반영되지 않으면 Reboot app을 누릅니다. 화면에 '공시목록 공유 저장 연결됨'이 표시돼야 합니다. 쓰기 권한은 첫 업로드 또는 복원 시 확인됩니다.

## 3. 이전 목록 복원과 공유 확인

개인 세션에만 남았던 목록은 설정을 바꿔도 자동으로 GitHub로 옮겨지지 않습니다. 이번에 받은 엑셀 자료는 복원 파일에 넣었습니다.

1. '이전에 올린 공시목록 불러오기' 버튼이 보이면 한 번 누릅니다.
2. 기존 공유 자료는 유지되고 없는 공시만 추가됩니다. 반복해도 중복 추가하지 않습니다.
3. 조회 날짜를 2026-10-06, 2026-10-07로 각각 선택해 HDC와 HS효성첨단소재를 확인합니다. 2026-10-02에서는 GKL과 KB금융을 확인합니다.
4. 다른 PC나 IP에서 같은 사이트 주소를 열고 같은 조회 날짜를 선택합니다.
5. 다른 사람이 새 파일을 올리고 '업로드한 목록 반영'을 누르면 공유 저장됩니다. 원래 화면은 30초 주기로 갱신하거나 '공유 목록 새로고침'을 누릅니다.

공시가 없는 날짜의 결과는 비어 있습니다. 전체를 보려면 '모든 날짜의 누적 결과 보기'를 체크하세요.

## 4. 평소 사용

KIND 공시목록 XLS/XLSX를 올리고 '업로드한 목록 반영'을 누릅니다. 기존에 없는 행만 추가합니다. 같은 분에 나온 다른 공시와 과거 날짜의 미등록 공시도 추가합니다. 결과 CSV에는 공시일시, 종목코드, 회사명, 제목, 제출인만 담습니다.

회사코드의 종목코드 대응과 외국법인 예외를 유지했습니다. 추가상장·변경상장 및 종속회사의 주요경영사항 제외 규칙도 유지했습니다.

현재 방식은 작은 팀에서 사용하는 GitHub 공통 저장 방식입니다. 접속자가 많아지거나 공시 JSON이 커지면 데이터베이스 전환이 필요할 수 있습니다. 로그인·사용자별 업로드 권한 구분은 이번 변경에 포함하지 않았습니다.

## 검증 범위

첨부 XLS 읽기·중복 병합·종목코드 필터링, 모의 GitHub 저장소에서 접속자별 순차 업데이트·동시 충돌 후 병합·저장 실패를 확인했습니다. 실제 사용자 GitHub 토큰으로 저장하거나 운영 사이트에 배포한 검증은 수행하지 않았습니다.

Streamlit 참고: https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment
GitHub API 참고: https://docs.github.com/en/rest/repos/contents
