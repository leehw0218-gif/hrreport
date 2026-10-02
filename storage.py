"""저장 계층 (PRD 4.7) - 서버의 JSON 파일에 저장한다

HTML 버전은 브라우저(localStorage)에 저장했지만, Streamlit은 서버에서 돌기 때문에
`data/hr_data.json` 파일에 저장한다. 바뀔 때마다 자동 저장하므로 새로고침해도 남는다.

주의: Streamlit Cloud에서는 앱이 다시 시작되거나 다시 배포되면 이 파일이 지워지고,
같은 앱을 여는 모든 사람이 같은 파일을 쓴다. 그래서 JSON 백업 파일을 꼭 받아 둔다.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import analysis as A
from . import model as M

APP_ID = "hr-transfer-tool"
SCHEMA_VERSION = 2  # HTML 버전과 같은 구조. 백업 파일을 서로 주고받을 수 있다

DATA_DIR = Path(os.environ.get("HR_TOOL_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_FILE = DATA_DIR / "hr_data.json"


def default_settings() -> dict:
    return {
        "deptTenureYears": 3,
        "relatedCareerMinYears": 1,
        "requiredCriteria": {"R1": False, "R2": False, "R3": False},
        "termWeights": dict(M.DEFAULT_TERM_WEIGHTS),
    }


def empty_state() -> dict:
    return {"version": SCHEMA_VERSION, "employees": [], "jobs": [], "settings": default_settings(),
            "reviews": [], "lastQuery": None, "savedAt": None, "lastBackupAt": None}


# ---------------------------------------------------------------------------
# 정리(sanitize)와 버전 이전(migrate)
# 정해진 필드만 남긴다. 모델에 없는 필드(평가점수 등)는 버린다 (P4). 빈 값은 채우지 않는다 (P6).
# ---------------------------------------------------------------------------

def _str_or_none(v):
    return v if isinstance(v, str) and v.strip() else None


def sanitize_employee(e: dict) -> dict:
    years = e.get("payGradeYears")
    valid_years = isinstance(years, int) and not isinstance(years, bool) and years >= 0
    return {
        "id": str(e.get("id")),
        "name": str(e.get("name")),
        "department": _str_or_none(e.get("department")),
        # v1의 grade(직급)는 이름만 바뀐 같은 값이라 그대로 옮긴다
        "payGrade": _str_or_none(e["payGrade"] if "payGrade" in e else e.get("grade")),
        "payGradeYears": years if valid_years else None,
        "jobTitle": _str_or_none(e.get("jobTitle")),
        "position": _str_or_none(e.get("position")),
        "hireDate": _str_or_none(e.get("hireDate")),
        "currentJob": e.get("currentJob") if isinstance(e.get("currentJob"), str) else "",
        "deptStartDate": _str_or_none(e.get("deptStartDate")),
        "jobHistory": [
            {"job": h["job"], "startDate": _str_or_none(h.get("startDate")),
             "endDate": _str_or_none(h.get("endDate")), "isCurrent": h.get("isCurrent") is True}
            for h in (e.get("jobHistory") or []) if isinstance(h, dict) and isinstance(h.get("job"), str)
        ],
        # v1처럼 시점 없는 리스트면 '시점 미지정'으로 둔다 (시점을 추정하지 않음)
        "desiredJobs": M.normalize_desired(e.get("desiredJobs")),
        "evaluations": M.normalize_evaluations(e.get("evaluations")),
    }


def sanitize_review(r: dict) -> dict:
    return {
        "employeeId": str(r.get("employeeId")),
        "targetJob": str(r.get("targetJob")),
        "status": r.get("status") if r.get("status") in M.REVIEW_STATUSES else "미검토",
        "memo": r.get("memo") if isinstance(r.get("memo"), str) else "",
        "updatedAt": _str_or_none(r.get("updatedAt")),
    }


def sanitize_query(q):
    if not isinstance(q, dict) or not isinstance(q.get("targetJob"), str):
        return None
    f = q.get("filters") or {}
    pay = M.clean_list(f.get("payGrades"))
    if not pay and isinstance(f.get("grade"), str) and f["grade"]:
        pay = [f["grade"]]  # v1 단일 직급 필터
    return {
        "targetJob": q["targetJob"],
        "sort": q.get("sort") if q.get("sort") in A.SORT_LABELS else "score",
        "filters": {
            "department": f.get("department") if isinstance(f.get("department"), str) else "",
            "payGrades": pay,
            "unknownOnly": f.get("unknownOnly") is True,
        },
    }


def _valid_years(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 40


def valid_term_weights(w: dict) -> bool:
    ok = all(isinstance(w.get(t), (int, float)) and not isinstance(w.get(t), bool) and 0 <= w[t] <= 1 for t in M.TERMS)
    return ok and w["short"] >= w["mid"] >= w["long"]


def migrate(data) -> tuple[dict, str | None, bool, bool]:
    """(state, warning, block_save, upgraded)"""
    if not isinstance(data, dict):
        return empty_state(), "저장된 데이터 형식이 올바르지 않아 빈 상태로 시작합니다.", False, False
    version = data.get("version") if isinstance(data.get("version"), int) else 1
    if version > SCHEMA_VERSION:
        return empty_state(), "이 도구보다 새 버전에서 저장한 데이터입니다. 덮어쓰지 않도록 저장을 중지했습니다.", True, False

    settings = default_settings()
    s = data.get("settings") or {}
    if _valid_years(s.get("deptTenureYears")):
        settings["deptTenureYears"] = s["deptTenureYears"]
    if _valid_years(s.get("relatedCareerMinYears")):
        settings["relatedCareerMinYears"] = s["relatedCareerMinYears"]
    for cid in ("R1", "R2", "R3"):
        settings["requiredCriteria"][cid] = (s.get("requiredCriteria") or {}).get(cid) is True
    if isinstance(s.get("termWeights"), dict) and valid_term_weights(s["termWeights"]):
        settings["termWeights"] = {t: s["termWeights"][t] for t in M.TERMS}

    state = {
        "version": SCHEMA_VERSION,
        "employees": [sanitize_employee(e) for e in data.get("employees") or [] if isinstance(e, dict)],
        "jobs": [{"name": str(j.get("name")), "relatedJobs": M.clean_list(j.get("relatedJobs"))}
                 for j in data.get("jobs") or [] if isinstance(j, dict)],
        "settings": settings,
        "reviews": [sanitize_review(r) for r in data.get("reviews") or [] if isinstance(r, dict)],
        "lastQuery": sanitize_query(data.get("lastQuery")),
        "savedAt": _str_or_none(data.get("savedAt")),
        "lastBackupAt": _str_or_none(data.get("lastBackupAt")),
    }
    return state, None, False, version < SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 파일 읽기/쓰기
# ---------------------------------------------------------------------------

def load() -> tuple[dict, str | None, bool]:
    """(state, warning, block_save). 예전 버전 파일이면 읽은 직후 새 구조로 저장한다"""
    if not DATA_FILE.exists():
        return empty_state(), None, False
    raw = DATA_FILE.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # 손상된 파일은 지우지 않고 다른 이름으로 보관한 뒤 빈 상태로 시작한다
        kept = DATA_FILE.with_name(f"hr_data.corrupt-{M.now_kst():%Y%m%d%H%M%S}.json")
        DATA_FILE.replace(kept)
        return empty_state(), f'저장된 데이터가 손상되어 읽지 못했습니다. 원본은 "{kept.name}" 파일로 보관했습니다.', False
    state, warning, block, upgraded = migrate(parsed)
    if upgraded and not block:
        save(state)
    return state, warning, block


def save(state: dict) -> None:
    """임시 파일에 쓴 뒤 바꿔치기한다. 쓰는 도중 앱이 꺼져도 기존 파일이 깨지지 않는다"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state["savedAt"] = M.now_kst().isoformat(timespec="seconds")
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".hr_data.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


# ---------------------------------------------------------------------------
# 백업 / 복원 (HTML 버전과 같은 형식이라 서로 주고받을 수 있다)
# ---------------------------------------------------------------------------

def backup_bytes(state: dict) -> bytes:
    data = {k: v for k, v in state.items() if k != "lastBackupAt"}
    payload = {"app": APP_ID, "version": SCHEMA_VERSION, "exportedAt": M.now_kst().isoformat(timespec="seconds"), "data": data}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def validate_backup(data) -> str | None:
    if not isinstance(data, dict):
        return "이 도구의 백업 파일이 아닙니다."
    if isinstance(data.get("version"), int) and data["version"] > SCHEMA_VERSION:
        return "이 도구보다 새 버전에서 만든 백업 파일입니다."
    if not isinstance(data.get("employees"), list) or not isinstance(data.get("jobs"), list):
        return "이 도구의 백업 파일이 아닙니다 (직원/직무 데이터 없음)."
    if any(not isinstance(e, dict) or not isinstance(e.get("id"), str) or not isinstance(e.get("name"), str)
           for e in data["employees"]):
        return "직원 데이터 형식이 올바르지 않습니다."
    if any(not isinstance(j, dict) or not isinstance(j.get("name"), str) for j in data["jobs"]):
        return "직무 데이터 형식이 올바르지 않습니다."
    return None


def parse_backup(raw: bytes) -> tuple[dict | None, str | None]:
    """(복원할 상태, 오류 메시지)"""
    try:
        parsed = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "JSON 형식이 아닌 파일입니다."
    data = parsed.get("data") if isinstance(parsed, dict) and parsed.get("app") == APP_ID else parsed
    problem = validate_backup(data)
    if problem:
        return None, problem
    state, _, _, _ = migrate(data)
    return state, None
