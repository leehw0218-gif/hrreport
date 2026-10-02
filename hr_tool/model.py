"""데이터 모델 공통 상수와 정규화 도우미 (HTML 버전 model.js와 같은 규칙)

- 값이 없으면 None/빈 리스트 그대로 둔다. 다른 값으로 채우지 않는다 (P6).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Pay Gr. (구 직급)
PAY_GRADES = ["G1", "G2", "G3", "G4", "R1", "R2", "R3", "R4"]
# 직위
POSITIONS = ["매니저", "책임매니저"]
# 직책 입력 예시 (자유 입력)
JOB_TITLE_EXAMPLES = ["팀원", "파트장", "팀장"]
# 인사평가 등급. 높은 순서
EVAL_GRADES = ["O", "E", "M", "N", "U"]
# 상세보기에서 보여 줄 인사평가 기간 (년)
EVAL_YEARS_SHOWN = 5

# 희망직무 시점. 앞일수록 가중치가 높다
TERMS = ["short", "mid", "long"]
TERM_LABELS = {"short": "단기", "mid": "중기", "long": "장기", "unspecified": "시점 미지정"}
DEFAULT_TERM_WEIGHTS = {"short": 1.0, "mid": 0.7, "long": 0.4}

REVIEW_STATUSES = ["미검토", "검토 중", "면담 예정", "제외"]

DISCLAIMER = "본 결과는 HR 담당자의 검토를 돕는 참고정보이며, 최종 인사 결정이 아닙니다."

# 서버(Streamlit Cloud)는 UTC로 돌기 때문에 날짜 계산은 한국 시간 기준으로 한다
KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(KST)


def clean_list(value) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in value if isinstance(value, list) else []:
        if isinstance(x, str) and x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def empty_desired() -> dict:
    return {"short": [], "mid": [], "long": [], "unspecified": []}


def normalize_desired(value) -> dict:
    """희망직무를 {short, mid, long, unspecified}로 맞춘다.

    예전(v1) 데이터처럼 시점 없이 리스트로 들어오면 unspecified(시점 미지정)에 둔다.
    시점을 임의로 정하지 않는다 (P6).
    """
    if isinstance(value, list):
        return {"short": [], "mid": [], "long": [], "unspecified": clean_list(value)}
    d = value if isinstance(value, dict) else {}
    return {k: clean_list(d.get(k)) for k in ("short", "mid", "long", "unspecified")}


def all_desired_jobs(value) -> list[str]:
    d = normalize_desired(value)
    return clean_list(d["short"] + d["mid"] + d["long"] + d["unspecified"])


def has_any_desired(value) -> bool:
    return len(all_desired_jobs(value)) > 0


def normalize_evaluations(value) -> list[dict]:
    """인사평가 정리: 연도가 없으면 버리고, 등급이 없으면 None. 연도마다 1건, 최신 순"""
    by_year: dict[int, dict] = {}
    for ev in value if isinstance(value, list) else []:
        if not isinstance(ev, dict):
            continue
        try:
            year = int(ev.get("year"))
        except (TypeError, ValueError):
            continue
        if isinstance(ev.get("year"), float) and not float(ev["year"]).is_integer():
            continue
        by_year[year] = {
            "year": year,
            "performance": ev.get("performance") if ev.get("performance") in EVAL_GRADES else None,
            "competency": ev.get("competency") if ev.get("competency") in EVAL_GRADES else None,
        }
    return [by_year[y] for y in sorted(by_year, reverse=True)]


# 소속: 본부 > 사업부 > 실 > 팀
ORG_LEVELS = [("hq", "본부"), ("division", "사업부"), ("office", "실"), ("team", "팀")]

# 사원번호 형식 (영문·숫자·하이픈 1~20자)
EMPLOYEE_NO_PATTERN = r"[A-Za-z0-9-]{1,20}"


def org_path(e: dict) -> str | None:
    """'본부 > 사업부 > 실 > 팀'. 빈 단계는 건너뛴다. 모두 비면 None"""
    parts = [e.get(k) for k, _ in ORG_LEVELS if e.get(k)]
    return " > ".join(parts) or None


def org_short(e: dict) -> str | None:
    """카드용 짧은 소속: 사업부(없으면 본부) · 실 · 팀"""
    head = e.get("division") or e.get("hq")
    parts = [p for p in (head, e.get("office"), e.get("team")) if p]
    return " · ".join(parts) or None


def scope_of(user: dict | None) -> dict | None:
    """조회 범위. 사용자가 속한 사업부 기준, 사업부가 비어 있으면 본부 기준.

    사업부 이름이 다른 본부에도 있을 수 있어 사업부 기준일 때는 본부까지 같이 맞춘다.
    본부·사업부가 모두 비어 있으면 None (범위를 추정하지 않는다, P6).
    """
    if not user:
        return None
    if user.get("division"):
        return {"level": "division", "hq": user.get("hq"), "division": user["division"],
                "label": " > ".join(p for p in (user.get("hq"), user["division"]) if p) + " (사업부 기준)"}
    if user.get("hq"):
        return {"level": "hq", "hq": user["hq"], "label": f"{user['hq']} (본부 기준, 사업부 미지정)"}
    return None


def in_scope(e: dict, scope: dict | None) -> bool:
    if not scope:
        return False
    if scope["level"] == "division":
        return e.get("division") == scope["division"] and e.get("hq") == scope["hq"]
    return e.get("hq") == scope["hq"]


def evaluation_years(employees: list[dict], now: datetime | None = None) -> list[int]:
    """상세보기에 보여 줄 연도 (최신 순 5개).

    기준 연도는 전체 직원 데이터에서 가장 최근 평가 연도. 평가 데이터가 없으면 작년.
    해당 연도 기록이 없는 직원은 그 연도를 "기록 없음"으로 보여 준다 (추정하지 않음).
    """
    years = [ev["year"] for e in employees for ev in e.get("evaluations", [])]
    latest = max(years) if years else (now or now_kst()).year - 1
    return [latest - i for i in range(EVAL_YEARS_SHOWN)]
