"""CSV 가져오기/내보내기 (PRD 4.4, 4.7, 6.6) - HTML 버전 csv.js와 같은 양식과 규칙

- 빈 값은 빈 값 그대로 저장한다 (P6)
- 인사평가는 '인사평가' 열로만 읽고 상세보기 참고용으로 저장한다. 그 밖의 평가·점수 열은 읽지 않는다 (P4)
"""
from __future__ import annotations

import csv
import io
import re

from . import analysis as A
from . import model as M

COLUMN_LABELS = {
    "id": "사원번호",
    "name": "이름",
    "hq": "본부",
    "division": "사업부",
    "office": "실",
    "team": "팀",
    "payGrade": "Pay Gr.",
    "payGradeYears": "Pay Gr. 년차",
    "jobTitle": "직책",
    "position": "직위",
    "hireDate": "당사입사일",
    "currentJob": "현재직무",
    "deptStartDate": "현부서배치일",
    "desiredShort": "희망직무(단기)",
    "desiredMid": "희망직무(중기)",
    "desiredLong": "희망직무(장기)",
    "desiredLegacy": "희망직무(시점 미지정)",
    "jobHistory": "직무이력",
    "evaluations": "인사평가",
}
EXPORT_KEYS = ["id", "name", "hq", "division", "office", "team", "payGrade", "payGradeYears", "jobTitle", "position", "hireDate",
               "currentJob", "deptStartDate", "desiredShort", "desiredMid", "desiredLong", "jobHistory", "evaluations"]
REQUIRED_KEYS = ["id", "name", "currentJob"]

# 열 이름(공백·점·괄호 제거, 소문자) → 열 키. 예전 열 이름(직급, 희망직무)도 받는다
HEADER_ALIASES = {
    "사원번호": "id", "사번": "id",
    "이름": "name",
    "본부": "hq",
    "사업부": "division",
    "실": "office",
    "팀": "team", "부서": "team",  # 예전 '부서' 열은 팀으로 받는다
    "paygr": "payGrade", "직급": "payGrade",
    "paygr년차": "payGradeYears", "직급년차": "payGradeYears",
    "직책": "jobTitle",
    "직위": "position",
    "당사입사일": "hireDate", "입사일": "hireDate",
    "현재직무": "currentJob",
    "현부서배치일": "deptStartDate",
    "희망직무단기": "desiredShort", "단기희망직무": "desiredShort",
    "희망직무중기": "desiredMid", "중기희망직무": "desiredMid",
    "희망직무장기": "desiredLong", "장기희망직무": "desiredLong",
    "희망직무": "desiredLegacy", "희망직무시점미지정": "desiredLegacy",
    "직무이력": "jobHistory",
    "인사평가": "evaluations",
}
SCORE_COLUMN_RE = re.compile(r"평가|점수|고과|성과|score|rating|kpi", re.I)
JOB_NAME_FORBIDDEN = [":", "|", ";", "~"]

TEMPLATE_EXAMPLE = ["100001", "김OO", "생산본부", "제1사업부", "생산실", "생산1팀", "G3", "3", "팀원", "책임매니저", "2018-03-01", "생산관리", "2021-08-01",
                    "구매관리", "생산기획", "", "생산기획:2018-03-01~2021-07-31|생산관리:2021-08-01~", "2025:E/M|2024:M/E"]


# ---------------------------------------------------------------------------
# 기본 처리
# ---------------------------------------------------------------------------

def decode(raw: bytes) -> tuple[str, str]:
    """UTF-8이 아니면 엑셀 기본 저장 형식(EUC-KR/CP949)으로 다시 읽는다"""
    try:
        return raw.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        return raw.decode("cp949", errors="replace"), "EUC-KR"


def parse(text: str) -> list[list[str]]:
    if text.startswith("﻿"):
        text = text[1:]
    return [row for row in csv.reader(io.StringIO(text))]


def _guard_formula(value: str) -> str:
    """엑셀에서 수식으로 실행되지 않도록 =, +, -, @ 로 시작하는 값 앞에 '를 붙인다"""
    return "'" + value if re.match(r"^[=+\-@\t\r]", value) else value


def stringify(rows: list[list]) -> bytes:
    """2차원 리스트 → CSV 바이트. 엑셀에서 한글이 깨지지 않도록 UTF-8 BOM을 붙인다 (F-63)"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    for r in rows:
        writer.writerow(["" if v is None else _guard_formula(str(v)) for v in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def forbidden_job_chars(name: str) -> list[str]:
    return [ch for ch in JOB_NAME_FORBIDDEN if ch in str(name)]


def is_future_date(value: str | None) -> bool:
    return bool(value) and value > A.today_string()


def looks_like_real_name(name: str) -> bool:
    """마스킹되지 않은 실명 형식인지 (한글 2~5글자). F-36"""
    return bool(re.fullmatch(r"[가-힣]{2,5}", str(name).strip()))


def _split_list(value: str, sep: str) -> list[str]:
    return M.clean_list([s.strip() for s in str(value or "").split(sep)])


def _normalize_header(h: str) -> str:
    return re.sub(r"[\s.()]", "", str(h)).lower()


# ---------------------------------------------------------------------------
# 직원 CSV
# ---------------------------------------------------------------------------

def _parse_history(value: str, current_job: str, errors: list[str]) -> list[dict]:
    items = [s.strip() for s in str(value or "").split("|") if s.strip()]
    result, current_count = [], 0
    for idx, item in enumerate(items):
        label = f'직무이력 {idx + 1}번째("{item}")'
        colon = item.find(":")
        if colon <= 0:
            errors.append(f'{label}: "직무:시작일~종료일" 형식이 아닙니다')
            continue
        job = item[:colon].strip()
        if not job:
            errors.append(f"{label}: 직무명이 비어 있습니다")
            continue
        bad = forbidden_job_chars(job)
        if bad:
            errors.append(f"{label}: 직무명에 {' '.join(bad)} 기호를 쓸 수 없습니다")
            continue
        period = item[colon + 1:]
        if "~" not in period:
            errors.append(f'{label}: 기간에 "~"가 없습니다')
            continue
        start, end = (x.strip() for x in period.split("~", 1))
        if start and not A.is_valid_date(start):
            errors.append(f'{label}: 시작일 "{start}"이 올바른 날짜(YYYY-MM-DD)가 아닙니다')
            continue
        if end and not A.is_valid_date(end):
            errors.append(f'{label}: 종료일 "{end}"이 올바른 날짜(YYYY-MM-DD)가 아닙니다')
            continue
        if start and end and A.parse_date(start) > A.parse_date(end):
            errors.append(f"{label}: 시작일이 종료일보다 늦습니다")
            continue
        if is_future_date(start) or is_future_date(end):
            errors.append(f"{label}: 오늘 이후 날짜는 쓸 수 없습니다")
            continue
        is_current = not end and job == current_job
        current_count += is_current
        result.append({"job": job, "startDate": start or None, "endDate": end or None, "isCurrent": is_current})
    if current_count > 1:
        errors.append(f"종료일이 빈 현재 직무 이력이 {current_count}건입니다 (1건만 가능)")
    return result


def _parse_evaluations(value: str, errors: list[str]) -> list[dict]:
    """'2025:E/M|2024:M/E' (연도:성과/역량). 등급을 비우면 None (예: '2025:E/')"""
    items = [s.strip() for s in str(value or "").split("|") if s.strip()]
    this_year = M.now_kst().year
    seen, result = set(), []
    for idx, item in enumerate(items):
        label = f'인사평가 {idx + 1}번째("{item}")'
        m = re.fullmatch(r"(\d{4})\s*:\s*([A-Za-z]?)\s*/\s*([A-Za-z]?)", item)
        if not m:
            errors.append(f'{label}: "연도:성과/역량" 형식이 아닙니다 (예: 2025:E/M)')
            continue
        year = int(m.group(1))
        perf = m.group(2).upper() or None
        comp = m.group(3).upper() or None
        if year < 1990 or year > this_year:
            errors.append(f"{label}: 연도가 올바르지 않습니다")
            continue
        if year in seen:
            errors.append(f"{label}: {year}년 평가가 두 번 있습니다")
            continue
        if any(g and g not in M.EVAL_GRADES for g in (perf, comp)):
            errors.append(f"{label}: 등급은 {' '.join(M.EVAL_GRADES)} 중 하나여야 합니다")
            continue
        if not perf and not comp:
            errors.append(f"{label}: 성과와 역량 등급이 모두 비어 있습니다")
            continue
        seen.add(year)
        result.append({"year": year, "performance": perf, "competency": comp})
    return M.normalize_evaluations(result)


def parse_employees(text: str, known_jobs: list[str]) -> dict:
    """직원 CSV를 읽어 검증한다. 저장은 하지 않는다."""
    out = {"fatal": None, "rows": [], "errors": [], "score_columns": [], "ignored_columns": [],
           "legacy_desired": False, "new_jobs": [], "name_warnings": []}
    table = [r for r in parse(text) if any(str(c).strip() for c in r)]
    if not table:
        out["fatal"] = "빈 파일입니다."
        return out

    index: dict[str, int] = {}
    for i, raw in enumerate(table[0]):
        key = HEADER_ALIASES.get(_normalize_header(raw))
        if key:
            index.setdefault(key, i)
        elif SCORE_COLUMN_RE.search(raw):
            out["score_columns"].append(raw)
        elif str(raw).strip():
            out["ignored_columns"].append(raw)

    missing = [COLUMN_LABELS[k] for k in REQUIRED_KEYS if k not in index]
    if missing:
        out["fatal"] = f"필수 열이 없습니다: {', '.join(missing)}. 양식 파일의 첫 줄(열 이름)을 확인해 주세요."
        return out
    if len(table) == 1:
        out["fatal"] = "열 이름만 있고 직원 데이터가 없습니다."
        return out
    out["legacy_desired"] = "desiredLegacy" in index

    new_jobs: dict[str, bool] = {}
    seen_ids: dict[str, int] = {}

    def cell(row, key):
        i = index.get(key)
        return "" if i is None or i >= len(row) else str(row[i]).strip()

    def check_date(value, label, messages):
        if not value:
            return
        if not A.is_valid_date(value):
            messages.append(f'{label} "{value}"이 올바른 날짜(YYYY-MM-DD)가 아닙니다')
        elif is_future_date(value):
            messages.append(f'{label} "{value}"이 오늘 이후 날짜입니다')

    for idx, row in enumerate(table[1:]):
        row_number = idx + 2  # 엑셀 기준 행 번호 (1행은 열 이름)
        messages: list[str] = []
        emp_no = cell(row, "id")
        name = cell(row, "name")
        current_job = cell(row, "currentJob")
        dept_start = cell(row, "deptStartDate")
        hire = cell(row, "hireDate")
        pay = cell(row, "payGrade").upper()
        pay_years = cell(row, "payGradeYears")
        position = cell(row, "position")

        if not emp_no:
            messages.append("사원번호가 비어 있습니다")
        elif not re.fullmatch(M.EMPLOYEE_NO_PATTERN, emp_no):
            messages.append(f'사원번호 "{emp_no}"는 영문·숫자·하이픈 20자 이내여야 합니다')
        elif emp_no in seen_ids:
            messages.append(f"사원번호 {emp_no}가 {seen_ids[emp_no]}행에도 있습니다 (중복)")
        if not name:
            messages.append("이름이 비어 있습니다")
        if not current_job:
            messages.append("현재직무가 비어 있습니다")
        check_date(dept_start, "현부서배치일", messages)
        check_date(hire, "당사입사일", messages)
        if pay and pay not in M.PAY_GRADES:
            messages.append(f'Pay Gr. "{pay}"은 {" ".join(M.PAY_GRADES)} 중 하나여야 합니다')
        if pay_years and not re.fullmatch(r"\d{1,2}", pay_years):
            messages.append(f'Pay Gr. 년차 "{pay_years}"은 0 이상의 정수여야 합니다')
        if position and position not in M.POSITIONS:
            messages.append(f'직위 "{position}"은 {", ".join(M.POSITIONS)} 중 하나여야 합니다')

        desired = {
            "short": _split_list(cell(row, "desiredShort"), ";"),
            "mid": _split_list(cell(row, "desiredMid"), ";"),
            "long": _split_list(cell(row, "desiredLong"), ";"),
            "unspecified": _split_list(cell(row, "desiredLegacy"), ";"),
        }
        all_desired = M.all_desired_jobs(desired)
        for job in [current_job] + all_desired:
            bad = forbidden_job_chars(job) if job else []
            if bad:
                messages.append(f'직무명 "{job}"에 {" ".join(bad)} 기호를 쓸 수 없습니다')
        history = _parse_history(cell(row, "jobHistory"), current_job, messages)
        evaluations = _parse_evaluations(cell(row, "evaluations"), messages)

        if emp_no and emp_no not in seen_ids:
            seen_ids[emp_no] = row_number
        if messages:
            out["errors"].append({"row_number": row_number, "messages": messages})
            continue

        for job in [current_job] + all_desired + [h["job"] for h in history]:
            if job and job not in known_jobs:
                new_jobs[job] = True
        if looks_like_real_name(name):
            out["name_warnings"].append({"row_number": row_number, "name": name})
        out["rows"].append({"row_number": row_number, "employee": {
            "id": emp_no,
            "name": name,
            "hq": cell(row, "hq") or None,
            "division": cell(row, "division") or None,
            "office": cell(row, "office") or None,
            "team": cell(row, "team") or None,
            "payGrade": pay or None,
            "payGradeYears": int(pay_years) if pay_years else None,
            "jobTitle": cell(row, "jobTitle") or None,
            "position": position or None,
            "hireDate": hire or None,
            "currentJob": current_job,
            "deptStartDate": dept_start or None,
            "jobHistory": history,
            "desiredJobs": desired,
            "evaluations": evaluations,
        }})
    out["new_jobs"] = list(new_jobs)
    return out


def _history_text(history: list[dict]) -> str:
    return "|".join(f"{h['job']}:{h.get('startDate') or ''}~{h.get('endDate') or ''}" for h in history or [])


def _evaluations_text(evals: list[dict]) -> str:
    return "|".join(f"{e['year']}:{e.get('performance') or ''}/{e.get('competency') or ''}" for e in evals or [])


def employees_to_csv(employees: list[dict]) -> bytes:
    """직원 목록을 가져오기 양식과 같은 형식으로 내보낸다 (다시 가져올 수 있음)"""
    has_legacy = any(M.normalize_desired(e.get("desiredJobs"))["unspecified"] for e in employees)
    keys = EXPORT_KEYS + (["desiredLegacy"] if has_legacy else [])
    rows = [[COLUMN_LABELS[k] for k in keys]]
    for e in employees:
        d = M.normalize_desired(e.get("desiredJobs"))
        values = {
            "id": e.get("id"), "name": e.get("name"), "hq": e.get("hq"), "division": e.get("division"),
            "office": e.get("office"), "team": e.get("team"), "payGrade": e.get("payGrade"),
            "payGradeYears": "" if e.get("payGradeYears") is None else str(e["payGradeYears"]),
            "jobTitle": e.get("jobTitle"), "position": e.get("position"), "hireDate": e.get("hireDate"),
            "currentJob": e.get("currentJob"), "deptStartDate": e.get("deptStartDate"),
            "desiredShort": ";".join(d["short"]), "desiredMid": ";".join(d["mid"]), "desiredLong": ";".join(d["long"]),
            "desiredLegacy": ";".join(d["unspecified"]),
            "jobHistory": _history_text(e.get("jobHistory")), "evaluations": _evaluations_text(e.get("evaluations")),
        }
        rows.append([values[k] or "" for k in keys])
    return stringify(rows)


def template_csv() -> bytes:
    return stringify([[COLUMN_LABELS[k] for k in EXPORT_KEYS], TEMPLATE_EXAMPLE])
