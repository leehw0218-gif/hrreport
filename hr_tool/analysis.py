"""후보 분석 로직 (PRD 5장) - HTML 버전 analysis.js와 같은 규칙, 화면과 분리한 순수 함수

- 입력값은 현재 직무, 과거 직무경력, 경력기간, 희망직무(단기·중기·장기), 현부서 근속만 쓴다 (P3).
- 인사평가는 입력에도 점수에도 정렬에도 쓰지 않는다. 상세보기 참고용일 뿐이다 (P4).
- 근거 판정은 반드시 'met'(충족) / 'unmet'(미충족) / 'unknown'(확인 불가) 중 하나다.
- 값이 없으면 기본값으로 채우지 않고 'unknown'으로 둔다 (P6).
  종료일 대신 오늘 날짜를 쓰는 경우는 is_current가 True인 이력 하나뿐이다.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import model as M

CRITERIA_LABELS = {"R1": "희망직무 일치", "R2": "유관 업무경력", "R3": "현부서 근속"}
RESULT_ICONS = {"met": "✓", "unmet": "✗", "unknown": "?"}
RESULT_LABELS = {"met": "충족", "unmet": "미충족", "unknown": "확인 불가"}
SORT_LABELS = {"score": "추천 점수", "career": "유관경력 기간", "tenure": "현부서 근속"}
SORTERS = {
    "score": ["score", "met_count", "related_career_months", "tenure_months"],
    "career": ["related_career_months", "score", "tenure_months"],
    "tenure": ["tenure_months", "score", "related_career_months"],
}

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


# ---------------------------------------------------------------------------
# 날짜와 기간
# ---------------------------------------------------------------------------

def parse_date(value) -> date | None:
    """'YYYY-MM-DD' → date. 형식이 틀리거나 없는 날짜(2024-13-45 등)면 None"""
    if not isinstance(value, str):
        return None
    m = _DATE_RE.match(value.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def is_valid_date(value) -> bool:
    return parse_date(value) is not None


def today_string() -> str:
    return M.now_kst().date().isoformat()


def months_inclusive(start: str | None, end: str | None) -> int | None:
    """시작일부터 종료일까지(종료일 포함) 꽉 찬 개월 수. 계산할 수 없으면 None

    예) 2018-03-01 ~ 2021-07-31 → 41개월 (3년 5개월)
    """
    s, e = parse_date(start), parse_date(end)
    if not s or not e or s > e:
        return None
    ex = e + timedelta(days=1)
    months = (ex.year - s.year) * 12 + (ex.month - s.month)
    if ex.day < s.day:
        months -= 1
    return months


def history_duration(hist: dict, today: str) -> tuple[int | None, str | None]:
    """직무 이력 한 건의 기간. (개월 수, 계산할 수 없는 이유)"""
    if not hist.get("startDate"):
        return None, "시작일 없음"
    end = hist.get("endDate")
    if not end:
        if hist.get("isCurrent"):
            end = today
        else:
            return None, "종료일 없음"
    if not is_valid_date(hist["startDate"]) or not is_valid_date(end):
        return None, "날짜 형식 오류"
    months = months_inclusive(hist["startDate"], end)
    if months is None:
        return None, "시작일이 종료일보다 늦음"
    return months, None


def format_months(months: int | None) -> str | None:
    if months is None:
        return None
    if months <= 0:
        return "1개월 미만"
    years, rest = divmod(months, 12)
    if years == 0:
        return f"{rest}개월"
    if rest == 0:
        return f"{years}년"
    return f"{years}년 {rest}개월"


def format_years(years: float) -> str:
    return format_months(round(years * 12)) or "0년"


def format_period(hist: dict) -> str:
    start = hist.get("startDate") or "시작일 없음"
    end = hist.get("endDate") or ("현재" if hist.get("isCurrent") else "종료일 없음")
    return f"{start} ~ {end}"


def format_weight(w: float) -> str:
    return f"{round(w * 100) / 100:.1f}"


# ---------------------------------------------------------------------------
# 근거 판정
# ---------------------------------------------------------------------------

def _criterion(cid: str, result: str, summary: str, steps: list[str], **extra) -> dict:
    return {"id": cid, "label": CRITERIA_LABELS[cid], "result": result, "summary": summary, "steps": steps, **extra}


def get_related_jobs(jobs: list[dict], target_job: str) -> list[str]:
    """직무 마스터에서 대상 직무의 유관직무. 담당자가 지정한 값만 쓴다"""
    for j in jobs or []:
        if j.get("name") == target_job:
            return list(j.get("relatedJobs") or [])
    return []


def term_weights_of(settings: dict) -> dict:
    return {**M.DEFAULT_TERM_WEIGHTS, **(settings.get("termWeights") or {})}


def evaluate_desired_job(employee: dict, target: str, weights: dict) -> dict:
    """R1. 희망직무 일치 (시점별 가중치)

    여러 시점에 같은 직무가 있으면 가중치가 가장 높은 시점(단기 → 중기 → 장기)을 쓴다.
    시점 미지정(예전 데이터)에만 있으면 가중치를 정할 수 없어 '확인 불가'로 둔다 (P6).
    """
    d = M.normalize_desired(employee.get("desiredJobs"))
    if not M.has_any_desired(d):
        return _criterion("R1", "unknown", "희망직무 데이터 없음", [
            "단기·중기·장기 희망직무가 모두 등록되어 있지 않습니다.",
            "데이터가 없어 판단하지 않았습니다 → 확인 불가",
        ], weight=0, term=None)

    steps = [
        f"{M.TERM_LABELS[t]} 희망직무 (가중치 {format_weight(weights[t])}): " + (", ".join(d[t]) if d[t] else "데이터 없음")
        for t in M.TERMS
    ]
    if d["unspecified"]:
        steps.append("시점 미지정 희망직무: " + ", ".join(d["unspecified"]))

    hits = [t for t in M.TERMS if target in d[t]]
    if hits:
        best = hits[0]
        steps.append(f"'{target}'이(가) " + "·".join(M.TERM_LABELS[t] for t in hits) +
                     f" 희망직무에 있습니다 → 충족 (가장 높은 {M.TERM_LABELS[best]} 가중치 {format_weight(weights[best])} 적용)")
        return _criterion("R1", "met", f"{target} {M.TERM_LABELS[best]} 희망", steps, weight=weights[best], term=best)

    if target in d["unspecified"]:
        steps.append(f"'{target}'이(가) 시점 미지정 희망직무에만 있어 가중치를 정할 수 없습니다 → 확인 불가")
        return _criterion("R1", "unknown", f"{target} 희망 시점 확인 불가 (시점 미지정)", steps, weight=0, term=None)

    empty = [M.TERM_LABELS[t] for t in M.TERMS if not d[t]]
    steps.append(f"'{target}'이(가) 등록된 희망직무에 없습니다 → 미충족" +
                 (f" ({'·'.join(empty)} 희망직무는 등록되어 있지 않음)" if empty else ""))
    return _criterion("R1", "unmet", f"{target} 희망 안 함", steps, weight=0, term=None)


def evaluate_related_career(employee: dict, target: str, related: list[str], min_years: float, today: str) -> dict:
    """R2. 유관 업무경력 (대상 직무 + 유관직무 경력 합계)"""
    min_months = round(min_years * 12)
    min_label = format_years(min_years)
    relevant = [target] + related
    history = employee.get("jobHistory") or []
    steps = [
        f"'{target}'의 유관직무 (직무 마스터 기준): " + (", ".join(related) if related else "지정 없음"),
        "대상 직무 자체의 경력도 유관경력에 포함합니다.",
    ]
    if not history:
        steps.append("직무 이력이 등록되어 있지 않아 판단하지 않았습니다 → 확인 불가")
        return _criterion("R2", "unknown", "직무 이력 데이터 없음", steps, careers=[], total_months=None)

    careers = []
    for h in history:
        if h.get("job") in relevant:
            months, reason = history_duration(h, today)
            careers.append({**h, "months": months, "reason": reason})
    if not careers:
        steps.append(f"직무 이력 {len(history)}건 중 대상 직무나 유관직무 경력이 없습니다 → 미충족")
        return _criterion("R2", "unmet", "유관 업무경력 없음", steps, careers=[], total_months=None)

    # 기간이 긴 순서로. 기간을 모르는 이력은 뒤로 (자바스크립트 안정 정렬과 같은 순서)
    careers.sort(key=lambda c: -(c["months"] if c["months"] is not None else -1))
    for c in careers:
        steps.append(f"{c['job']} ({format_period(c)}) → " +
                     (format_months(c["months"]) if c["months"] is not None else f"기간 확인 불가: {c['reason']}"))

    known = [c for c in careers if c["months"] is not None]
    unknown = [c for c in careers if c["months"] is None]
    total = sum(c["months"] for c in known)

    if known and total >= min_months:
        longest = known[0]
        steps.append(f"확인된 기간 합계 {format_months(total)} ≥ 기준 {min_label} → 충족")
        if unknown:
            steps.append(f"날짜가 없는 이력 {len(unknown)}건은 합계에서 뺐습니다.")
        return _criterion("R2", "met", f"유관 업무경력 존재 ({longest['job']} {format_months(longest['months'])})",
                          steps, careers=careers, total_months=total)

    if unknown:
        prefix = f"확인된 기간 합계 {format_months(total)}로 기준 {min_label}에 못 미치지만, " if known else ""
        steps.append(prefix + "날짜가 없는 이력이 있어 판단하지 않았습니다 → 확인 불가")
        names = ", ".join(c["job"] for c in unknown)
        return _criterion("R2", "unknown", f"이력 날짜 없음으로 경력기간 확인 불가 ({names})",
                          steps, careers=careers, total_months=total if known else None)

    longest = known[0]
    steps.append(f"기간 합계 {format_months(total)} < 기준 {min_label} → 미충족")
    return _criterion("R2", "unmet", f"유관 업무경력 {min_label} 미만 ({longest['job']} {format_months(longest['months'])})",
                      steps, careers=careers, total_months=total)


def evaluate_tenure(employee: dict, min_years: float, today: str) -> dict:
    """R3. 현부서 근속"""
    min_months = round(min_years * 12)
    min_label = format_years(min_years)
    start = employee.get("deptStartDate")
    if not start:
        return _criterion("R3", "unknown", "현부서 배치일 데이터 없음", [
            "현부서 배치일이 등록되어 있지 않습니다.",
            "데이터가 없어 판단하지 않았습니다 → 확인 불가",
        ], tenure_months=None)
    months = months_inclusive(start, today)
    if months is None:
        return _criterion("R3", "unknown", "현부서 배치일 확인 불가 (날짜 오류)", [
            f"현부서 배치일: {start}",
            "날짜 형식이 잘못되었거나 오늘 이후 날짜라 계산하지 않았습니다 → 확인 불가",
        ], tenure_months=None)
    steps = [f"현부서 배치일: {start} → 오늘({today})까지 {format_months(months)}"]
    if months >= min_months:
        steps.append(f"{format_months(months)} ≥ 기준 {min_label} → 충족")
        return _criterion("R3", "met", f"현부서 {min_label} 이상 ({format_months(months)})", steps, tenure_months=months)
    steps.append(f"{format_months(months)} < 기준 {min_label} → 미충족")
    return _criterion("R3", "unmet", f"현부서 근속 {min_label} 미만 ({format_months(months)})", steps, tenure_months=months)


def score_of(criteria: list[dict], weights: dict) -> dict:
    """추천 점수 = 희망직무(시점 가중치) + 유관 업무경력(1.0) + 현부서 근속(1.0). 충족한 근거만 더한다 (평가점수 제외, P4)"""
    parts = []
    for c in criteria:
        value = 0 if c["result"] != "met" else (c["weight"] if c["id"] == "R1" else 1)
        note = M.TERM_LABELS[c["term"]] if c["id"] == "R1" and c.get("term") else None
        parts.append({"id": c["id"], "label": c["label"], "value": value, "note": note})
    total = round(sum(p["value"] for p in parts) * 100) / 100
    return {"total": total, "max": round((weights["short"] + 2) * 100) / 100, "parts": parts}


def evaluate_employee(employee: dict, target: str, ctx: dict) -> dict:
    weights = term_weights_of(ctx["settings"])
    related = get_related_jobs(ctx["jobs"], target)
    r1 = evaluate_desired_job(employee, target, weights)
    r2 = evaluate_related_career(employee, target, related, ctx["settings"]["relatedCareerMinYears"], ctx["today"])
    r3 = evaluate_tenure(employee, ctx["settings"]["deptTenureYears"], ctx["today"])
    criteria = [r1, r2, r3]
    score = score_of(criteria, weights)
    unknown_count = sum(1 for c in criteria if c["result"] == "unknown")
    return {
        "employee": employee,
        "criteria": criteria,
        "met_count": sum(1 for c in criteria if c["result"] == "met"),
        "unknown_count": unknown_count,
        "needs_check": unknown_count > 0,
        "score": score["total"],
        "score_detail": score,
        "careers": r2["careers"],
        "related_career_months": r2["total_months"],
        "tenure_months": r3["tenure_months"],
    }


# ---------------------------------------------------------------------------
# 후보 목록 (PRD 5.3, 5.4)
# ---------------------------------------------------------------------------

def sort_results(results: list[dict], sort_key: str) -> list[dict]:
    """정렬. 값이 없는 항목은 뒤로. 평가점수 정렬은 없다 (P4)"""
    keys = SORTERS.get(sort_key, SORTERS["score"])

    def key(r):
        return tuple(-(r[k] if r[k] is not None else -1) for k in keys) + (str(r["employee"]["id"]),)
    return sorted(results, key=key)


def analyze(employees: list[dict], target: str, ctx: dict) -> dict:
    required = ctx["settings"].get("requiredCriteria") or {}
    out = {"target_job": target, "candidates": [], "undetermined": [], "excluded_current": [], "excluded_by_required": []}
    for e in employees or []:
        if e.get("currentJob") == target:
            out["excluded_current"].append(e)
            continue
        r = evaluate_employee(e, target, ctx)
        # 필수 조건은 '미충족'일 때만 뺀다. '확인 불가'는 빼는 이유로 쓰지 않는다 (PRD 5.3)
        if any(required.get(c["id"]) and c["result"] == "unmet" for c in r["criteria"]):
            out["excluded_by_required"].append(r)
        elif r["met_count"] >= 1:
            out["candidates"].append(r)
        elif r["unknown_count"] > 0:
            out["undetermined"].append(r)
    out["candidates"] = sort_results(out["candidates"], "score")
    return out
