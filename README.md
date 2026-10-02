# 전환배치 후보자 탐색 (Streamlit 버전)

HR 인력운영 담당자가 전환배치 후보를 찾고 검토할 때 참고하는 도구입니다.
**최종 인사 결정이 아니라 검토를 돕는 참고정보**를 보여 줍니다.

- 희망직무(단기·중기·장기 가중치), 유관 업무경력, 현부서 근속으로 추천 점수를 계산하고 근거를 함께 보여 줍니다.
- 인사평가는 상세보기에서 참고용으로만 보여 주며, 추천·정렬·필터에는 쓰지 않습니다.
- 데이터가 없는 항목은 추정하지 않고 "확인 불가"로 표시합니다.

## 로그인과 권한

| 구분 | 할 수 있는 일 | 조회 범위 |
|---|---|---|
| 관리자 | 모든 탭: 후보자 탐색, 직원 관리(CSV 업로드), 직무 마스터, 분석 기준 설정, 사용자 관리, 백업 | 전체 직원 |
| 일반 사용자 | 후보자 탐색, 상세보기, 검토 상태·메모 입력 | 자기 **사업부** 소속원 (사업부가 비어 있으면 **본부**), 본인 제외 |

- 아이디는 **사원번호**, 처음 비밀번호도 사원번호입니다. 첫 로그인 때 새 비밀번호(8자 이상, 영문+숫자)로 바꾸게 합니다.
- 5번 연속 틀리면 5분 동안 잠깁니다. 관리자가 **사용자 관리** 탭에서 잠금 해제·비밀번호 초기화·관리자 권한 부여를 할 수 있습니다.
- 비밀번호는 PBKDF2-SHA256 해시로 `data/auth.json`에 저장하며 원문은 저장하지 않습니다. 계정 정보는 백업 파일에 들어가지 않습니다.
- 새로고침하면 다시 로그인해야 합니다.

### 관리자 설정 (Secrets)

`.streamlit/secrets.toml.example`을 참고합니다.

- 내 PC: 같은 폴더에 `secrets.toml`로 복사해 씁니다 (GitHub에 올라가지 않음).
- Streamlit Cloud: 앱의 **Settings → Secrets**에 붙여 넣습니다.

```toml
[auth]
admin_ids = ["관리자 사원번호"]
require_password_change = true
```

## 내 PC에서 실행

```bash
pip install -r requirements.txt
streamlit run app.py --server.address localhost
```

Windows에서는 `run_local.bat`을 더블클릭해도 됩니다. 브라우저에서 http://localhost:8501 이 열립니다.

## 데이터 저장

- 직원·직무·검토 기록은 `data/hr_data.json`에 자동 저장됩니다. 새로고침해도 남습니다.
- `data/` 폴더는 `.gitignore`에 들어 있어 GitHub에 올라가지 않습니다.
- 왼쪽 메뉴의 **백업 파일 받기**로 JSON 백업을 받아 두세요. HTML 버전과 같은 형식이라 서로 옮길 수 있습니다.

## Streamlit Community Cloud 배포

1. 이 폴더의 내용을 GitHub 저장소 **최상위**에 올립니다 (아래 "올릴 파일").
2. https://share.streamlit.io 에서 **Create app** → 저장소와 브랜치를 고르고 Main file path에 `app.py`를 입력합니다.
3. Advanced settings에서 Python 버전을 고릅니다 (3.12 이상 권장).
4. Deploy를 누릅니다.

### 올릴 파일

```
app.py
requirements.txt
README.md
.gitignore
.streamlit/config.toml
.streamlit/secrets.toml.example
hr_tool/__init__.py
hr_tool/auth.py
hr_tool/model.py
hr_tool/analysis.py
hr_tool/csv_io.py
hr_tool/storage.py
hr_tool/sample_data.json
static/PretendardVariable.woff2
static/Pretendard-LICENSE.txt
```

`static/` 폴더가 없으면 글꼴이 기본 글꼴로 보입니다. **`data/`와 `.streamlit/secrets.toml`은 절대 올리지 않습니다** (직원 데이터, 계정 정보, 관리자 설정). `__pycache__/`도 올리지 않습니다. `run_local.bat`, `tests/`는 배포에 필요하지 않습니다.

### 배포 전에 알아 둘 점

- **저장 데이터가 사라질 수 있습니다.** Streamlit Cloud는 앱이 잠들었다 깨거나 다시 배포되면 서버 파일을 지웁니다. 쓰기 전에 백업 파일을 복원하고, 끝나면 백업 파일을 받아 두세요.
- **계정 정보도 함께 사라집니다.** 앱이 다시 시작되면 `data/auth.json`도 지워져 모든 사람의 비밀번호가 처음 비밀번호(사원번호)로 돌아갑니다. 실제 운영 전에는 외부 저장소(DB)로 옮겨야 합니다.
- **공개 범위를 확인하세요.** 공개 저장소로 배포한 앱은 링크를 아는 누구나 열 수 있습니다. 비공개(private) 저장소로 배포한 뒤 앱 설정의 Sharing에서 볼 수 있는 사람을 지정하세요.
- **실제 개인정보는 넣지 마세요.** 개발·시연 단계에서는 가상 샘플 데이터만 씁니다.

## 글꼴

본문과 제목에 [Pretendard](https://github.com/orioncactus/pretendard)(v1.3.9, SIL Open Font License 1.1)를 씁니다. 글꼴 파일을 `static/`에 포함해 외부 서버에서 받지 않습니다. 라이선스 전문은 `static/Pretendard-LICENSE.txt`에 있습니다.
