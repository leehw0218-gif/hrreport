"""전환배치 후보자 탐색 도구 - Streamlit 버전

실행: streamlit run app.py
- 분석·CSV·저장 로직은 hr_tool/ 에 있고, 이 파일은 화면만 맡는다.
- 데이터는 data/hr_data.json 에 자동 저장한다 (새로고침해도 유지).
- 사용자 입력은 화면에 넣을 때 마크다운 특수문자를 이스케이프한다 (md 함수).
- 로그인: 사원번호로 로그인한다. 관리자는 전체 데이터를 다루고, 그 외 사용자는 자기 사업부(없으면 본부) 소속원만
  조회하고 검토 상태·메모를 입력한다. 권한 확인은 화면을 숨기는 것과 별도로 데이터를 바꾸는 함수 안에서도 한다.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd
import streamlit as st

from hr_tool import analysis as A
from hr_tool import auth
from hr_tool import csv_io
from hr_tool import model as M
from hr_tool import storage

st.set_page_config(page_title="전환배치 후보자 탐색", page_icon="🧭", layout="wide")

st.markdown("""
<style>
  .block-container { padding-top: 2rem; }
  /* Pretendard는 자간을 살짝 좁히면 한글이 더 단정하게 읽힌다 */
  html, body, [class*="st-"], button, input, textarea { letter-spacing: -0.01em; }
  h1, h2, h3, h4 { letter-spacing: -0.025em; }
  /* 한글이 "2개" / "월"처럼 낱말 중간에서 줄바꿈되지 않게 한다 */
  [data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] *,
  [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * { word-break: keep-all !important; overflow-wrap: anywhere; }
  div[data-testid="stVerticalBlockBorderWrapper"] p { margin-bottom: 0.25rem; }
</style>
""", unsafe_allow_html=True)

CARDS_PER_PAGE = 30
MEMO_MAX = 2000
BACKUP_REMIND_DAYS = 7
MISSING = ":gray[*데이터 없음*]"
ICON_MD = {"met": ":green[**✓ 충족**]", "unmet": ":gray[**✗ 미충족**]", "unknown": ":orange[**? 확인 불가**]"}

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~:$])")


def md(text) -> str:
    """사용자 입력을 마크다운에 넣을 때 특수문자를 글자 그대로 보이게 한다"""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def md_or_missing(value) -> str:
    return MISSING if value in (None, "", []) else md(value)


def fmt_dt(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone(M.KST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return None


def file_stamp() -> str:
    return M.now_kst().strftime("%Y%m%d_%H%M")


# ---------------------------------------------------------------------------
# 로그인 사용자와 권한
# ---------------------------------------------------------------------------

def auth_config() -> dict:
    """배포 설정(Secrets)의 [auth]. 없으면 관리자 없음 + 첫 로그인 때 비밀번호 변경"""
    try:
        sec = dict(st.secrets.get("auth", {}))
    except Exception:  # secrets.toml이 없는 환경
        sec = {}
    return {"admin_ids": [str(x).strip() for x in sec.get("admin_ids", []) if str(x).strip()],
            "require_change": bool(sec.get("require_password_change", True))}


def current_user(state: dict | None = None) -> dict | None:
    """로그인한 사용자. 실행할 때마다 파일에서 권한과 소속을 다시 계산한다 (권한이 바뀌면 바로 반영)"""
    uid = st.session_state.get("auth_user")
    if not uid:
        return None
    if state is None:
        state = storage.load()[0]
    cfg = auth_config()
    data = auth.load()
    if not auth.account_exists(uid, {e["id"] for e in state["employees"]}, data, cfg["admin_ids"]):
        return None  # 직원 목록에서 빠졌거나 관리자 권한이 회수됨
    emp = find_employee(state, uid)
    admin = auth.is_admin(uid, data, cfg["admin_ids"])
    return {"id": uid, "name": emp["name"] if emp else uid, "employee": emp, "is_admin": admin,
            "config_admin": uid in cfg["admin_ids"], "scope": None if admin else M.scope_of(emp)}


def visible_employees(state: dict, user: dict | None) -> list[dict]:
    """관리자는 전체, 그 외에는 같은 사업부(없으면 본부) 소속원. 본인은 빼고 보여 준다"""
    if not user:
        return []
    if user["is_admin"]:
        return state["employees"]
    return [e for e in state["employees"] if e["id"] != user["id"] and M.in_scope(e, user["scope"])]


def can_view(state: dict, user: dict | None, emp_id: str) -> bool:
    return any(e["id"] == emp_id for e in visible_employees(state, user))


def require_admin() -> bool:
    user = current_user()
    if user and user["is_admin"]:
        return True
    st.session_state["_flash"] = ("error", "관리자만 할 수 있는 작업입니다.")
    return False


def logout():
    for k in list(st.session_state.keys()):
        del st.session_state[k]


# ---------------------------------------------------------------------------
# 데이터 읽기/쓰기
# 실행할 때마다 파일에서 새로 읽는다. 바꿀 때는 mutate()로 읽기 → 바꾸기 → 저장을 한 번에 한다.
# ---------------------------------------------------------------------------

def load_state():
    return storage.load()


def mutate(fn, message: str | None = None) -> bool:
    state, _, block = storage.load()
    if block:
        st.session_state["_flash"] = ("error", "저장이 중지된 상태라 바꾸지 못했습니다.")
        return False
    fn(state)
    storage.save(state)
    if message:
        st.toast(message)
    return True


def ctx_of(state: dict) -> dict:
    return {"jobs": state["jobs"], "settings": state["settings"], "today": A.today_string()}


def job_names(state: dict) -> list[str]:
    return [j["name"] for j in state["jobs"]]


def find_employee(state: dict, emp_id: str):
    return next((e for e in state["employees"] if e["id"] == emp_id), None)


def find_review(state: dict, emp_id: str, target: str):
    return next((r for r in state["reviews"] if r["employeeId"] == emp_id and r["targetJob"] == target), None)


def upsert_review(state: dict, emp_id: str, target: str, **patch) -> None:
    r = find_review(state, emp_id, target)
    if r is None:
        r = {"employeeId": emp_id, "targetJob": target, "status": "미검토", "memo": "", "updatedAt": None, "updatedBy": None}
        state["reviews"].append(r)
    r.update(patch, updatedAt=M.now_kst().isoformat(timespec="seconds"), updatedBy=st.session_state.get("auth_user"))


def job_usage_count(state: dict, name: str) -> int:
    return sum(1 for e in state["employees"]
               if e["currentJob"] == name or name in M.all_desired_jobs(e["desiredJobs"])
               or any(h["job"] == name for h in e["jobHistory"]))


def criteria_text(settings: dict) -> str:
    w = A.term_weights_of(settings)
    text = (f"현부서 근속 {A.format_years(settings['deptTenureYears'])} 이상 · 유관경력 {A.format_years(settings['relatedCareerMinYears'])} 이상"
            " · 희망 시점 가중치 " + " · ".join(f"{M.TERM_LABELS[t]} {A.format_weight(w[t])}" for t in M.TERMS))
    required = [A.CRITERIA_LABELS[c] for c in ("R1", "R2", "R3") if settings["requiredCriteria"].get(c)]
    if required:
        text += " · 필수 조건: " + ", ".join(required)
    return text


def desired_summary(desired) -> str | None:
    d = M.normalize_desired(desired)
    parts = [f"{M.TERM_LABELS[t]} {', '.join(d[t])}" for t in M.TERMS + ["unspecified"] if d[t]]
    return " · ".join(parts) or None


def career_summary(result: dict) -> str | None:
    if not result["employee"]["jobHistory"]:
        return None
    careers = result["careers"] or []
    if not careers:
        return "없음"
    shown = ", ".join(f"{c['job']} {A.format_months(c['months']) if c['months'] is not None else '기간 확인 불가'}"
                      for c in careers[:2])
    return f"{shown} 외 {len(careers) - 2}건" if len(careers) > 2 else shown


# ---------------------------------------------------------------------------
# 공통 영역: 사이드바(저장 상태, 백업), 안내
# ---------------------------------------------------------------------------

def record_backup():
    if not require_admin():
        return
    mutate(lambda s: s.update(lastBackupAt=M.now_kst().isoformat(timespec="seconds")), "백업 파일을 내려받았습니다.")


def load_sample():
    if not require_admin():
        return
    sample = storage.migrate(_sample_data())[0]

    def apply(s):
        s.update(jobs=sample["jobs"], employees=sample["employees"], reviews=[], userQueries={})
    mutate(apply, "가상 샘플 데이터를 불러왔습니다.")
    reset_search_widgets()


@st.cache_data
def _sample_data() -> dict:
    import json
    from pathlib import Path
    return json.loads((Path(__file__).parent / "hr_tool" / "sample_data.json").read_text(encoding="utf-8"))


def scope_text(user: dict) -> str:
    if user["is_admin"]:
        return "전체 직원 (관리자)"
    return user["scope"]["label"] if user["scope"] else "조회 범위 없음 (소속 정보 없음)"


def render_sidebar(state: dict, user: dict) -> None:
    with st.sidebar:
        st.markdown("### 🧭 전환배치 후보자 탐색")
        st.markdown(f"**{md(user['name'])}** ({md(user['id'])})  \n" +
                    (":blue-badge[관리자]" if user["is_admin"] else ":gray-badge[일반 사용자]"))
        st.caption("조회 범위: " + scope_text(user))
        c1, c2 = st.columns(2)
        with c1.popover("비밀번호 변경", width="stretch"):
            render_password_form(user["id"], forced=False)
        c2.button("로그아웃", on_click=logout, width="stretch", key="sb_logout")
        st.divider()
        if user["is_admin"]:
            st.caption(f"직원 {len(state['employees'])}명 · 직무 {len(state['jobs'])}개")
            st.caption("마지막 저장: " + (fmt_dt(state.get("savedAt")) or "기록 없음"))
            st.caption("마지막 백업: " + (fmt_dt(state.get("lastBackupAt")) or "기록 없음"))
            st.download_button("백업 파일 받기 (JSON)", storage.backup_bytes(state), f"전환배치도구_백업_{file_stamp()}.json",
                               "application/json", on_click=record_backup, width="stretch", key="sb_backup")
            st.caption("데이터는 이 앱이 실행 중인 서버의 파일(data/hr_data.json)에 저장됩니다. "
                       "Streamlit Cloud에서는 앱이 다시 시작되면 지워질 수 있으니 백업 파일을 받아 두세요.")
        st.caption("실제 개인정보는 입력하지 마세요. 이름은 \"김OO\"처럼 가려서 입력합니다.")


# ---------------------------------------------------------------------------
# 로그인, 비밀번호 변경
# ---------------------------------------------------------------------------

def render_login(state: dict) -> None:
    cfg = auth_config()
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.title("전환배치 후보자 탐색")
        st.caption("사원번호로 로그인하세요. 처음 비밀번호는 사원번호입니다.")
        with st.form("login_form"):
            emp_no = st.text_input("사원번호", max_chars=20)
            password = st.text_input("비밀번호", type="password", max_chars=100)
            submitted = st.form_submit_button("로그인", type="primary", width="stretch")
        if submitted:
            ok, reason, must_change = auth.verify(emp_no, password, {e["id"] for e in state["employees"]},
                                                  cfg["admin_ids"], cfg["require_change"])
            if ok:
                st.session_state["auth_user"] = emp_no.strip()
                st.session_state["must_change"] = must_change
                st.rerun()
            st.error(reason)
        if not cfg["admin_ids"]:
            st.warning("관리자가 설정되지 않았습니다. 배포 설정(Secrets)의 [auth] admin_ids에 관리자 사원번호를 등록하세요.")
        st.caption("본 도구는 HR 담당자의 검토를 돕는 참고정보를 제공하며, 최종 인사 결정을 하지 않습니다.")


def change_password(emp_no: str, prefix: str):
    current = st.session_state.get(prefix + "current", "")
    new = st.session_state.get(prefix + "new", "")
    confirm = st.session_state.get(prefix + "confirm", "")
    if not auth.check_password(emp_no, current):
        st.session_state[prefix + "error"] = ("현재 비밀번호가 맞지 않습니다. 이미 비밀번호를 바꿨다면 처음 비밀번호(사원번호)가 아니라 "
                                              "바꾼 비밀번호를 입력하세요. 기억나지 않으면 관리자에게 초기화를 요청하세요.")
        return
    problem = auth.password_problem(emp_no, new) or (None if new == confirm else "새 비밀번호가 서로 다릅니다.")
    if problem:
        st.session_state[prefix + "error"] = problem
        return
    auth.set_password(emp_no, new)
    st.session_state["must_change"] = False
    for k in ("current", "new", "confirm"):
        st.session_state[prefix + k] = ""
    # 몇 초 뒤 사라지는 알림만으로는 바뀐 것을 놓치기 쉬워 화면 위에도 남긴다
    st.session_state["_flash"] = ("success", "비밀번호를 바꿨습니다. 다음 로그인부터는 새 비밀번호를 쓰세요. "
                                             "처음 비밀번호(사원번호)로는 더 이상 로그인할 수 없습니다.")
    st.toast("비밀번호를 바꿨습니다.")


def render_password_form(emp_no: str, forced: bool) -> None:
    prefix = "pw_forced::" if forced else "pw::"
    rec = auth.load()["users"].get(emp_no, {})
    if not forced:
        st.caption("지금 비밀번호: " + ("처음 비밀번호(사원번호)" if not rec.get("hash")
                                        else f"{fmt_dt(rec.get('passwordChangedAt')) or ''}에 바꾼 비밀번호"))
    st.text_input("현재 비밀번호" + (" (처음이면 사원번호)" if forced else ""), type="password", key=prefix + "current")
    st.text_input("새 비밀번호", type="password", key=prefix + "new",
                  help=f"{auth.MIN_PASSWORD_LENGTH}자 이상, 영문과 숫자를 함께 쓰고, 사원번호와 달라야 합니다.")
    st.text_input("새 비밀번호 확인", type="password", key=prefix + "confirm")
    error = st.session_state.pop(prefix + "error", None)
    if error:
        st.error(error)
    st.button("비밀번호 바꾸기", type="primary", on_click=change_password, args=(emp_no, prefix), key=prefix + "submit")


def render_forced_password_change(user: dict) -> None:
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.title("비밀번호 변경")
        st.info(f"{md(user['name'])}님, 처음 로그인하셨거나 관리자가 비밀번호를 초기화했습니다. 새 비밀번호를 정해 주세요.")
        render_password_form(user["id"], forced=True)
        st.button("로그아웃", on_click=logout, key="forced_logout")


def render_notices(state: dict, warning: str | None, block: bool, user: dict) -> None:
    if warning:
        st.warning("저장 경고: " + warning)
    if block:
        st.error("저장이 중지된 상태입니다. 데이터를 바꿔도 저장되지 않습니다.")
    flash = st.session_state.pop("_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    if user["is_admin"] and state["employees"]:
        last = state.get("lastBackupAt")
        days = (M.now_kst() - datetime.fromisoformat(last)).days if last else None
        if days is None or days > BACKUP_REMIND_DAYS:
            st.info("데이터는 서버 파일에만 저장됩니다. 사라질 수 있으니 왼쪽 메뉴에서 백업 파일을 받아 두세요."
                    if days is None else f"마지막 백업 후 {BACKUP_REMIND_DAYS}일이 지났습니다. 백업 파일을 새로 받아 두세요.", icon="💾")


# ---------------------------------------------------------------------------
# 후보자 탐색 (PRD 4.1 ~ 4.3)
# ---------------------------------------------------------------------------

SEARCH_WIDGET_PREFIXES = ("q_sort", "q_div", "q_team", "q_pay", "q_unknown", "shown_count")
EMPTY_FILTERS = {"division": "", "team": "", "payGrades": [], "unknownOnly": False}


def my_query(state: dict) -> dict | None:
    return state.get("userQueries", {}).get(st.session_state.get("auth_user"))


def reset_search_widgets():
    for k in list(st.session_state.keys()):
        if k.startswith(SEARCH_WIDGET_PREFIXES):
            del st.session_state[k]


def run_search():
    target = st.session_state["target_select"]
    uid = st.session_state.get("auth_user")

    def apply(s):
        prev = s.setdefault("userQueries", {}).get(uid) or {}
        s["userQueries"][uid] = {"targetJob": target, "sort": prev.get("sort", "score"), "filters": dict(EMPTY_FILTERS)}
    mutate(apply)
    reset_search_widgets()


def update_query(**patch):
    uid = st.session_state.get("auth_user")

    def apply(s):
        q = s.get("userQueries", {}).get(uid)
        if not q:
            return
        if "sort" in patch:
            q["sort"] = patch["sort"]
        q["filters"].update(patch.get("filters", {}))
    mutate(apply)
    st.session_state.pop("shown_count", None)


def set_status(emp_id: str, target: str, key: str):
    state = storage.load()[0]
    if not can_view(state, current_user(state), emp_id):  # 조회 범위 밖 직원은 검토 입력 불가
        st.session_state["_flash"] = ("error", "조회 범위 밖의 직원입니다.")
        return
    mutate(lambda s: upsert_review(s, emp_id, target, status=st.session_state[key]))


def render_search(state: dict, user: dict) -> None:
    names = job_names(state)
    q = my_query(state)
    pool = visible_employees(state, user)
    if not user["is_admin"] and not user["scope"]:
        st.warning("내 소속(본부·사업부) 정보가 없어 조회할 수 있는 직원이 없습니다. 관리자에게 소속 등록을 요청하세요.")
        return
    if not names:
        st.info("직무 마스터에 등록된 직무가 없습니다. 직무 마스터 탭에서 직무를 추가하거나 가상 샘플 데이터를 불러오세요.")
        return

    # 검색한 직무가 직무 마스터에서 지워졌어도 선택지에 남겨 결과와 같은 직무를 가리키게 한다
    options = names + ([q["targetJob"]] if q and q["targetJob"] not in names else [])
    default = options.index(q["targetJob"]) if q else 0
    c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
    c1.selectbox("희망직무", options, index=default, key="target_select",
                 format_func=lambda j: j if j in names else f"{j} (직무 마스터에서 삭제됨)")
    c2.button("후보자 찾기", type="primary", on_click=run_search, width="stretch")
    st.info(M.DISCLAIMER, icon="ℹ️")
    st.caption(f"조회 범위: {scope_text(user)} · {len(pool)}명" + ("" if user["is_admin"] else " (본인 제외)"))

    if not q:
        st.caption("희망직무를 고르고 [후보자 찾기]를 누르면 추천 후보와 판단 근거를 보여줍니다.")
        return

    target = q["targetJob"]
    result = A.analyze(pool, target, ctx_of(state))
    f = q["filters"]
    filtered = [r for r in result["candidates"]
                if (not f["division"] or (r["employee"]["division"] or "") == f["division"])
                and (not f["team"] or (r["employee"]["team"] or "") == f["team"])
                and (not f["payGrades"] or r["employee"]["payGrade"] in f["payGrades"])
                and (not f["unknownOnly"] or r["needs_check"])]
    candidates = A.sort_results(filtered, q["sort"])

    st.caption("적용 기준: " + criteria_text(state["settings"]))
    total = len(result["candidates"])
    st.markdown(f"#### '{md(target)}' 추천 후보 :blue[**{len(candidates)}명**]" +
                ("" if len(candidates) == total else f" (필터 적용, 전체 {total}명 중)"))

    divisions = sorted({r["employee"]["division"] for r in result["candidates"] if r["employee"]["division"]})
    teams = sorted({r["employee"]["team"] for r in result["candidates"] if r["employee"]["team"]})
    pay_options = sorted({r["employee"]["payGrade"] for r in result["candidates"] if r["employee"]["payGrade"]} | set(f["payGrades"]),
                         key=lambda g: M.PAY_GRADES.index(g) if g in M.PAY_GRADES else 99)
    sort_keys = list(A.SORT_LABELS)
    k1, k2, k2b, k3, k4, k5 = st.columns([1.1, 1.3, 1.3, 1.8, 1.1, 1.1], vertical_alignment="bottom")
    k1.selectbox("정렬", sort_keys, index=sort_keys.index(q["sort"]), format_func=A.SORT_LABELS.get, key="q_sort",
                 on_change=lambda: update_query(sort=st.session_state["q_sort"]))
    div_options = [""] + divisions
    k2.selectbox("사업부", div_options, index=div_options.index(f["division"]) if f["division"] in div_options else 0,
                 format_func=lambda d: d or "전체", key="q_div",
                 on_change=lambda: update_query(filters={"division": st.session_state["q_div"]}))
    team_options = [""] + teams
    k2b.selectbox("팀", team_options, index=team_options.index(f["team"]) if f["team"] in team_options else 0,
                  format_func=lambda d: d or "전체", key="q_team",
                  on_change=lambda: update_query(filters={"team": st.session_state["q_team"]}))
    k3.multiselect("Pay Gr. (복수 선택)", pay_options, default=[g for g in f["payGrades"] if g in pay_options],
                   placeholder="전체", key="q_pay",
                   on_change=lambda: update_query(filters={"payGrades": sorted(
                       st.session_state["q_pay"], key=lambda g: M.PAY_GRADES.index(g) if g in M.PAY_GRADES else 99)}))
    k4.checkbox("확인 필요 후보만", value=f["unknownOnly"], key="q_unknown",
                on_change=lambda: update_query(filters={"unknownOnly": st.session_state["q_unknown"]}))
    k5.download_button("CSV 내보내기", results_csv(state, target, candidates), f"추천후보_{target}_{file_stamp()}.csv",
                       "text/csv", disabled=not candidates, width="stretch", on_click="ignore")

    notes = []
    if target not in names:
        notes.append(f":orange['{md(target)}' 직무가 직무 마스터에 없어 유관직무 없이 분석했습니다.]")
    if result["excluded_current"]:
        notes.append(f"이미 '{md(target)}' 직무에서 일하는 직원 {len(result['excluded_current'])}명은 후보에서 뺐습니다.")
    if result["excluded_by_required"]:
        notes.append(f"필수 조건을 미충족한 직원 {len(result['excluded_by_required'])}명은 후보에서 뺐습니다.")
    notes.append("추천 점수 = 희망직무(시점 가중치) + 유관 업무경력 1.0 + 현부서 근속 1.0. 충족한 근거만 더하며, 인사평가는 점수와 정렬에 쓰지 않습니다.")
    st.caption("  \n".join("• " + n for n in notes))

    if not candidates:
        st.info("필터 조건에 맞는 추천 후보가 없습니다." if total else "근거를 1개 이상 충족한 추천 후보가 없습니다.")
    else:
        shown = st.session_state.get("shown_count", CARDS_PER_PAGE)
        cols = st.columns(3)
        for i, r in enumerate(candidates[:shown]):
            with cols[i % 3]:
                render_card(state, r, target)
        if len(candidates) > shown:
            if st.button(f"더 보기 ({shown}/{len(candidates)}명 표시 중)", width="stretch"):
                st.session_state["shown_count"] = shown + CARDS_PER_PAGE
                st.rerun()

    if result["undetermined"]:
        with st.expander(f"판단 보류 {len(result['undetermined'])}명: 충족한 근거는 없지만 데이터가 없어 확인하지 못한 항목이 있는 직원"):
            for r in A.sort_results(result["undetermined"], "score"):
                e = r["employee"]
                c1, c2 = st.columns([5, 1], vertical_alignment="center")
                unknowns = ", ".join("? " + c["summary"] for c in r["criteria"] if c["result"] == "unknown")
                c1.markdown(f"{md(e['name'])} / {md_or_missing(e['currentJob'])} / {md_or_missing(M.org_short(e))} — :orange[{md(unknowns)}]")
                if c2.button("상세보기", key=f"undet_detail::{e['id']}"):
                    show_detail(e["id"], target)


def render_card(state: dict, r: dict, target: str) -> None:
    e = r["employee"]
    review = find_review(state, e["id"], target)
    status = review["status"] if review else "미검토"
    with st.container(border=True):
        badges = f":blue-background[추천 점수 **{A.format_weight(r['score'])}** / {A.format_weight(r['score_detail']['max'])}]"
        if r["needs_check"]:
            badges += " :orange-background[확인 필요]"
        if status != "미검토":
            badges += {"검토 중": " :blue-badge[검토 중]", "면담 예정": " :green-badge[면담 예정]", "제외": " :gray-badge[제외]"}[status]
        st.markdown(f"**{md(e['name'])} / {md_or_missing(e['currentJob'])} / {md_or_missing(e['payGrade'])}**  \n{badges}")
        st.markdown(
            f":gray[소속] {md_or_missing(M.org_short(e))}  \n"
            f":gray[유관경력] {md_or_missing(career_summary(r))}  \n"
            f":gray[희망직무] {md_or_missing(desired_summary(e['desiredJobs']))}  \n"
            f":gray[현부서 근속] {md_or_missing(A.format_months(r['tenure_months']))}")
        st.markdown("**후보 근거**  \n" + "  \n".join(f"{ICON_MD[c['result']]} {md(c['summary'])}" for c in r["criteria"]))
        c1, c2 = st.columns([3, 2], vertical_alignment="bottom")
        key = f"status::{e['id']}::{target}"
        c1.selectbox("검토상태", M.REVIEW_STATUSES, index=M.REVIEW_STATUSES.index(status), key=key,
                     on_change=set_status, args=(e["id"], target, key))
        if c2.button("상세보기", key=f"detail::{e['id']}", width="stretch"):
            show_detail(e["id"], target)


def results_csv(state: dict, target: str, candidates: list[dict]) -> bytes:
    """검토 결과 CSV. 인사평가는 넣지 않는다 (추천 근거가 아니므로, P4)"""
    rows = [[f"※ {M.DISCLAIMER}"],
            [f"대상 직무: {target}", f"적용 기준: {criteria_text(state['settings'])}", f"내보낸 일시: {M.now_kst():%Y-%m-%d %H:%M}"],
            [],
            ["대상직무", "사원번호", "이름", "본부", "사업부", "실", "팀", "Pay Gr.", "직위", "현재직무", "추천 점수", "충족 근거 수",
             "희망직무 근거", "유관경력 근거", "현부서 근속 근거", "확인 필요", "검토 상태", "메모", "검토 수정 일시"]]
    for r in candidates:
        e = r["employee"]
        rv = find_review(state, e["id"], target)
        rows.append([target, e["id"], e["name"], e["hq"] or "", e["division"] or "", e["office"] or "", e["team"] or "",
                     e["payGrade"] or "", e["position"] or "",
                     e["currentJob"], A.format_weight(r["score"]), r["met_count"]]
                    + [f"{A.RESULT_ICONS[c['result']]} {A.RESULT_LABELS[c['result']]}: {c['summary']}" for c in r["criteria"]]
                    + ["예" if r["needs_check"] else "", rv["status"] if rv else "미검토", rv["memo"] if rv else "",
                       fmt_dt(rv["updatedAt"]) or "" if rv else ""])
    return csv_io.stringify(rows)


# ---------------------------------------------------------------------------
# 후보 상세보기 (모달)
# ---------------------------------------------------------------------------

def save_review_from_dialog(emp_id: str, target: str):
    state = storage.load()[0]
    if not can_view(state, current_user(state), emp_id):
        return
    status = st.session_state[f"dlg_status::{emp_id}::{target}"]
    memo = st.session_state[f"dlg_memo::{emp_id}::{target}"]
    mutate(lambda s: upsert_review(s, emp_id, target, status=status, memo=memo))
    st.session_state.pop(f"status::{emp_id}::{target}", None)  # 카드의 검토상태도 새 값으로


@st.dialog("후보 상세보기", width="large")
def show_detail(emp_id: str, target: str) -> None:
    state, _, _ = load_state()
    e = find_employee(state, emp_id)
    if not e or not can_view(state, current_user(state), emp_id):
        st.error("직원을 찾을 수 없거나 조회 범위 밖의 직원입니다.")
        return
    today = A.today_string()
    r = A.evaluate_employee(e, target, ctx_of(state))
    review = find_review(state, emp_id, target)

    st.markdown(f"### {md(e['name'])}")
    st.caption(M.DISCLAIMER)

    st.markdown("##### 인적 정보")
    hire = e["hireDate"]
    hire_text = None
    if hire:
        months = A.months_inclusive(hire, today)
        hire_text = f"{hire} (근속 {A.format_months(months)})" if months is not None else hire
    profile = [("사원번호", e["id"]), ("대상 직무", target), ("본부", e["hq"]), ("사업부", e["division"]),
               ("실", e["office"]), ("팀", e["team"]), ("현재 직무", e["currentJob"]), ("현부서 배치일", e["deptStartDate"]),
               ("Pay Gr.", e["payGrade"]), ("Pay Gr. 년차", None if e["payGradeYears"] is None else f"{e['payGradeYears']}년차"),
               ("직책", e["jobTitle"]), ("직위", e["position"]), ("당사입사일", hire_text)]
    pc = st.columns(2)
    for i, (label, value) in enumerate(profile):
        pc[i % 2].markdown(f":gray[{label}] {md_or_missing(value)}")

    st.markdown("##### 희망직무 (시점별)")
    d = M.normalize_desired(e["desiredJobs"])
    w = A.term_weights_of(state["settings"])
    r1 = r["criteria"][0]
    rows = [{"시점": M.TERM_LABELS[t], "희망직무": ", ".join(d[t]) or "데이터 없음",
             "가중치": A.format_weight(w[t]) + (" (적용)" if r1["result"] == "met" and r1.get("term") == t else "")} for t in M.TERMS]
    if d["unspecified"]:
        rows.append({"시점": M.TERM_LABELS["unspecified"], "희망직무": ", ".join(d["unspecified"]), "가중치": "가중치 없음"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    st.markdown("##### 근거별 판단 과정")
    s = r["score_detail"]
    formula = " + ".join(f"{p['label']}{'(' + p['note'] + ')' if p['note'] else ''} {A.format_weight(p['value'])}" for p in s["parts"])
    st.info(f"**추천 점수 {A.format_weight(s['total'])} / {A.format_weight(s['max'])}** = {formula}. 미충족·확인 불가는 0으로 계산합니다.")
    for c in r["criteria"]:
        with st.container(border=True):
            st.markdown(f"{ICON_MD[c['result']]} **{c['label']}**")
            st.markdown("\n".join(f"{i}. {md(step)}" for i, step in enumerate(c["steps"], 1)))

    st.markdown("##### 직무 이력")
    if e["jobHistory"]:
        hist_rows = []
        for h in e["jobHistory"]:
            months, reason = A.history_duration(h, today)
            hist_rows.append({"직무": h["job"], "시작일": h["startDate"] or "데이터 없음",
                              "종료일": h["endDate"] or ("현재" if h["isCurrent"] else "데이터 없음"),
                              "기간": A.format_months(months) if months is not None else f"확인 불가 ({reason})"})
        st.dataframe(pd.DataFrame(hist_rows), hide_index=True, width="stretch")
    else:
        st.markdown(":gray[*직무 이력 데이터 없음*]")

    st.markdown(f"##### 인사평가 (최근 {M.EVAL_YEARS_SHOWN}년, 최신 순)")
    st.caption(f"참고용입니다. 추천 점수, 정렬, 필터에는 쓰지 않습니다. 등급: {' > '.join(M.EVAL_GRADES)} (5등급)")
    by_year = {ev["year"]: ev for ev in e["evaluations"]}
    eval_rows = []
    for y in M.evaluation_years(state["employees"]):
        ev = by_year.get(y)
        eval_rows.append({"연도": f"{y}년",
                          "성과": (ev["performance"] or "없음") if ev else "기록 없음",
                          "역량": (ev["competency"] or "없음") if ev else "기록 없음"})
    st.dataframe(pd.DataFrame(eval_rows), hide_index=True, width="stretch")

    st.markdown("##### 담당자 검토")
    status_key, memo_key = f"dlg_status::{emp_id}::{target}", f"dlg_memo::{emp_id}::{target}"
    st.selectbox("검토 상태", M.REVIEW_STATUSES,
                 index=M.REVIEW_STATUSES.index(review["status"]) if review else 0, key=status_key,
                 on_change=save_review_from_dialog, args=(emp_id, target))
    st.text_area("메모", value=review["memo"] if review else "", max_chars=MEMO_MAX, key=memo_key,
                 placeholder="면담 일정, 확인할 내용 등을 적어 두세요. 입력칸 밖을 누르면 자동 저장됩니다.",
                 on_change=save_review_from_dialog, args=(emp_id, target))
    st.caption("수정 일시: " + (fmt_dt(review["updatedAt"]) + (f" · 입력자 {md(review['updatedBy'])}" if review.get("updatedBy") else "")
                              if review and review["updatedAt"] else "아직 검토 기록이 없습니다."))
    if st.button("저장하고 닫기", type="primary"):
        save_review_from_dialog(emp_id, target)
        st.rerun()


# ---------------------------------------------------------------------------
# 직원 관리 (PRD 4.4)
# ---------------------------------------------------------------------------

def delete_employee(emp_id: str):
    if not require_admin():
        return
    def apply(s):
        s["employees"] = [e for e in s["employees"] if e["id"] != emp_id]
        s["reviews"] = [r for r in s["reviews"] if r["employeeId"] != emp_id]
    mutate(apply, "직원을 삭제했습니다.")


def apply_import(parsed: dict, mode: str):
    """mode: 'upsert'(같은 사원번호는 새 값으로 바꾸고 없으면 추가, 검토 기록 유지) / 'replace'(전체 교체)"""
    if not require_admin():
        return
    counts = {"added": 0, "updated": 0}

    def apply(s):
        if mode == "replace":
            s["employees"], s["reviews"] = [], []
        known = job_names(s)
        for name in parsed["new_jobs"]:
            if name not in known:
                s["jobs"].append({"name": name, "relatedJobs": []})
        index = {e["id"]: i for i, e in enumerate(s["employees"])}
        for row in parsed["rows"]:
            emp = row["employee"]
            if emp["id"] in index:
                s["employees"][index[emp["id"]]] = emp
                counts["updated"] += 1
            else:
                index[emp["id"]] = len(s["employees"])
                s["employees"].append(emp)
                counts["added"] += 1
    mutate(apply)
    st.toast(f"추가 {counts['added']}명 · 갱신 {counts['updated']}명")
    st.session_state["uploader_n"] = st.session_state.get("uploader_n", 0) + 1  # 업로드 칸 비우기


def render_import(state: dict) -> None:
    n = st.session_state.get("uploader_n", 0)
    file = st.file_uploader("CSV 가져오기 (UTF-8 또는 엑셀 기본 형식 EUC-KR)", type=["csv"], key=f"uploader_{n}")
    if not file:
        return
    text, encoding = csv_io.decode(file.getvalue())
    p = csv_io.parse_employees(text, job_names(state))
    with st.container(border=True):
        st.markdown(f"**CSV 가져오기 확인: {md(file.name)}**")
        if p["fatal"]:
            st.error(p["fatal"])
            return
        st.markdown(f"정상 **{len(p['rows'])}행** · 오류 **{len(p['errors'])}행**")
        if encoding != "UTF-8":
            st.caption(f"파일을 {encoding} 인코딩으로 읽었습니다. 한글이 올바른지 확인해 주세요.")
        if p["score_columns"]:
            st.caption("양식에 없는 평가·점수 열은 사용하지 않아 무시했습니다 (인사평가는 \"인사평가\" 열로만 받습니다): " + ", ".join(p["score_columns"]))
        if p["ignored_columns"]:
            st.caption("양식에 없는 열은 무시했습니다: " + ", ".join(p["ignored_columns"]))
        if p["legacy_desired"]:
            st.warning("시점이 없는 \"희망직무\" 열은 \"시점 미지정\"으로 가져옵니다. 추천에서는 \"확인 불가\"로 처리되니 단기·중기·장기 열로 나눠 주세요.")
        if p["new_jobs"]:
            st.caption(f"직무 마스터에 없는 직무 {len(p['new_jobs'])}개를 새로 추가합니다 (유관직무는 직접 지정해 주세요): " + ", ".join(p["new_jobs"]))
        if p["name_warnings"]:
            st.warning("실명으로 보이는 이름이 있습니다. 실제 개인정보는 사용하지 마세요: " +
                       ", ".join(f"{w['row_number']}행 {w['name']}" for w in p["name_warnings"]))
        if p["errors"]:
            st.dataframe(pd.DataFrame([{"행 번호": err["row_number"], "오류 내용": " / ".join(err["messages"])} for err in p["errors"]]),
                         hide_index=True, width="stretch")
            if p["rows"]:
                st.caption("오류가 있는 행은 빼고 정상 행만 반영합니다.")
        if p["rows"]:
            existing_ids = {e["id"] for e in state["employees"]}
            n_update = sum(1 for r in p["rows"] if r["employee"]["id"] in existing_ids)
            st.caption(f"같은 사원번호가 이미 있는 {n_update}명은 CSV 내용으로 바뀌고, 나머지 {len(p['rows']) - n_update}명은 새로 추가됩니다. "
                       "검토 기록은 그대로 남습니다.")
            c1, c2, _ = st.columns([1, 1, 2])
            c1.button(f"정상 {len(p['rows'])}명 추가·갱신", type="primary", on_click=apply_import, args=(p, "upsert"), width="stretch")
            with c2.popover("기존 직원을 모두 지우고 바꾸기", width="stretch"):
                st.warning(f"기존 직원 {len(state['employees'])}명과 검토 기록을 모두 지우고 CSV의 {len(p['rows'])}명으로 바꿉니다.")
                st.button("바꾸기", type="primary", on_click=apply_import, args=(p, "replace"), key="import_replace_confirm")


def render_employees(state: dict) -> None:
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("직원 추가", type="primary", width="stretch"):
        employee_form(None)
    c2.download_button("CSV 양식 내려받기", csv_io.template_csv(), "직원_가져오기_양식.csv", "text/csv",
                       width="stretch", on_click="ignore")
    c3.download_button("직원 목록 CSV 내보내기", csv_io.employees_to_csv(state["employees"]), f"직원목록_{file_stamp()}.csv",
                       "text/csv", disabled=not state["employees"], width="stretch", on_click="ignore")
    with c4.popover("가상 샘플 데이터 불러오기", width="stretch"):
        st.caption("현재 직원, 직무, 검토 기록을 모두 지우고 가상 샘플 데이터(직원 30명, 직무 8개)로 바꿉니다.")
        st.button("샘플로 바꾸기", type="primary", on_click=load_sample, key="sample_confirm_emp")
    st.caption("실제 개인정보(실명, 주민번호, 연락처)는 입력하지 마세요. 인사평가는 상세보기 참고용으로만 저장하며 추천에는 쓰지 않습니다.")

    render_import(state)

    if not state["employees"]:
        st.info("등록된 직원이 없습니다.")
        return
    keyword = st.text_input("직원 검색", placeholder="사원번호, 이름, 소속, Pay Gr., 직무로 찾기", key="emp_filter").strip().lower()
    employees = [e for e in state["employees"] if not keyword or any(
        v and keyword in str(v).lower()
        for v in [e["id"], e["name"], e["hq"], e["division"], e["office"], e["team"], e["payGrade"], e["jobTitle"], e["position"], e["currentJob"],
                  *M.all_desired_jobs(e["desiredJobs"])])]
    st.caption(f"검색 결과 {len(employees)}명 / 전체 {len(state['employees'])}명" if keyword else f"전체 {len(employees)}명")
    df = pd.DataFrame([{
        "사원번호": e["id"], "이름": e["name"], "소속": M.org_path(e) or "데이터 없음",
        "Pay Gr.": (e["payGrade"] or "데이터 없음") + (f" ({e['payGradeYears']}년차)" if e["payGradeYears"] is not None else ""),
        "직책 / 직위": f"{e['jobTitle'] or '데이터 없음'} / {e['position'] or '데이터 없음'}",
        "현재직무": e["currentJob"] or "데이터 없음", "현부서 배치일": e["deptStartDate"] or "데이터 없음",
        "희망직무": desired_summary(e["desiredJobs"]) or "데이터 없음",
        "직무 이력": "\n".join(f"{h['job']} ({A.format_period(h)})" for h in e["jobHistory"]) or "데이터 없음",
    } for e in employees])
    st.caption("표에서 한 줄을 고르면 수정·삭제할 수 있습니다.")
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="emp_table")
    rows = event.selection.rows if event and event.selection else []
    if rows and rows[0] < len(employees):
        e = employees[rows[0]]
        c1, c2, _ = st.columns([1, 1, 4])
        if c1.button(f"{e['name']}({e['id']}) 수정", width="stretch"):
            employee_form(e["id"])
        with c2.popover("삭제", width="stretch"):
            reviews = sum(1 for r in state["reviews"] if r["employeeId"] == e["id"])
            st.warning(f"{e['name']}({e['id']}) 직원을 삭제합니다." + (f" 검토 기록 {reviews}건도 함께 삭제됩니다." if reviews else ""))
            st.button("삭제하기", type="primary", on_click=delete_employee, args=(e["id"],), key=f"del_confirm::{e['id']}")


def _iso(value) -> str | None:
    if value is None or (not isinstance(value, (date, str)) and pd.isna(value)):
        return None
    if isinstance(value, str):
        return value or None
    return value.isoformat()[:10]


def _to_date(value: str | None):
    d = A.parse_date(value) if value else None
    return d


@st.dialog("직원 정보", width="large")
def employee_form(emp_id: str | None) -> None:
    state, _, _ = load_state()
    existing = find_employee(state, emp_id) if emp_id else None
    e = existing or {"id": "", "name": "", "hq": None, "division": None, "office": None, "team": None, "payGrade": None, "payGradeYears": None, "jobTitle": None, "position": None,
                     "hireDate": None, "currentJob": "", "deptStartDate": None, "jobHistory": [], "desiredJobs": M.empty_desired(),
                     "evaluations": []}
    d = M.normalize_desired(e["desiredJobs"])
    # 직무 마스터에 없는 기존 값도 선택지에 남겨 둔다 (지우지 않음)
    choices = sorted(set(job_names(state) + [e["currentJob"]] + M.all_desired_jobs(d) + [h["job"] for h in e["jobHistory"]]) - {""})
    pay_choices = M.PAY_GRADES + ([e["payGrade"]] if e["payGrade"] and e["payGrade"] not in M.PAY_GRADES else [])
    pos_choices = M.POSITIONS + ([e["position"]] if e["position"] and e["position"] not in M.POSITIONS else [])
    k = f"ef::{emp_id or 'new'}::"
    today = M.now_kst().date()

    st.markdown(f"### {'직원 수정: ' + md(e['name']) if existing else '직원 추가'}")
    st.markdown("**인적 정보**")
    c1, c2 = st.columns(2)
    emp_no = c1.text_input("사원번호 *", value=e["id"], max_chars=20, disabled=bool(existing), key=k + "empno",
                           help="로그인 아이디로 쓰입니다. 저장한 뒤에는 바꿀 수 없습니다." if not existing else "사원번호는 바꿀 수 없습니다.")
    name = c2.text_input("이름 *", value=e["name"], max_chars=30, placeholder="예: 김OO", key=k + "name")
    st.caption("소속: 조회 권한은 사업부(비어 있으면 본부) 기준으로 정해집니다.")
    oc = st.columns(4)
    org = {key: oc[i].text_input(label, value=e[key] or "", max_chars=50, key=k + "org_" + key)
           for i, (key, label) in enumerate(M.ORG_LEVELS)}
    c1, c2 = st.columns(2)
    pay = c1.selectbox("Pay Gr.", [""] + pay_choices, index=([""] + pay_choices).index(e["payGrade"] or ""),
                       format_func=lambda v: v or "선택 안 함", key=k + "pay")
    pay_years = c2.number_input("Pay Gr. 년차", min_value=0, max_value=50, step=1, value=e["payGradeYears"], key=k + "payyears")
    title = c1.text_input("직책", value=e["jobTitle"] or "", max_chars=20, placeholder="예: " + ", ".join(M.JOB_TITLE_EXAMPLES), key=k + "title")
    position = c2.selectbox("직위", [""] + pos_choices, index=([""] + pos_choices).index(e["position"] or ""),
                            format_func=lambda v: v or "선택 안 함", key=k + "position")
    hire = c1.date_input("당사입사일", value=_to_date(e["hireDate"]), min_value=date(1970, 1, 1), max_value=today, key=k + "hire")
    dept_start = c2.date_input("현부서 배치일", value=_to_date(e["deptStartDate"]), min_value=date(1970, 1, 1), max_value=today, key=k + "deptstart")
    current = c1.selectbox("현재 직무 *", [""] + choices, index=([""] + choices).index(e["currentJob"]) if e["currentJob"] in choices else 0,
                           format_func=lambda v: v or "선택하세요", key=k + "current")

    st.markdown("**희망직무 (시점별, 여러 개 선택 가능)**")
    w = A.term_weights_of(state["settings"])
    st.caption("추천할 때 단기 희망을 가장 높게 반영합니다. 가중치는 분석 기준 설정에서 바꿀 수 있습니다.")
    desired_inputs = {}
    tc = st.columns(3)
    for i, t in enumerate(M.TERMS):
        desired_inputs[t] = tc[i].multiselect(f"{M.TERM_LABELS[t]} (가중치 {A.format_weight(w[t])})", choices,
                                              default=[j for j in d[t] if j in choices], placeholder="없음", key=k + "desired_" + t)
    if d["unspecified"]:
        st.warning(f"시점 미지정 희망직무(이전 데이터): {', '.join(d['unspecified'])}. 위에서 시점을 지정하면 저장할 때 이 목록에서 빠집니다. "
                   "지정하지 않으면 그대로 남고, 추천에서는 \"확인 불가\"로 처리합니다.")

    st.markdown("**직무 이력**")
    st.caption("모르는 날짜는 비워 두세요. 비운 날짜는 추정하지 않고 \"확인 불가\"로 처리합니다. 맨 아래 빈 줄에 입력하면 줄이 추가됩니다.")
    hist_df = pd.DataFrame([{"직무": h["job"], "시작일": _to_date(h["startDate"]), "종료일": _to_date(h["endDate"]), "현재": h["isCurrent"]}
                            for h in e["jobHistory"]], columns=["직무", "시작일", "종료일", "현재"])
    hist_df["현재"] = hist_df["현재"].astype("bool")
    for col in ("시작일", "종료일"):  # 빈 표에서도 날짜 칸으로 편집되도록
        hist_df[col] = pd.to_datetime(hist_df[col])
    hist_edit = st.data_editor(hist_df, num_rows="dynamic", hide_index=True, width="stretch", key=k + "history",
                               column_config={
                                   "직무": st.column_config.SelectboxColumn("직무", options=choices, required=True),
                                   "시작일": st.column_config.DateColumn("시작일", min_value=date(1970, 1, 1), max_value=today, format="YYYY-MM-DD"),
                                   "종료일": st.column_config.DateColumn("종료일", min_value=date(1970, 1, 1), max_value=today, format="YYYY-MM-DD"),
                                   "현재": st.column_config.CheckboxColumn("현재", default=False)})

    st.markdown("**인사평가 (참고용)**")
    st.caption(f"상세보기에서 최근 {M.EVAL_YEARS_SHOWN}년을 최신 순으로 보여 줍니다. 추천 점수, 정렬, 필터에는 쓰지 않습니다. 등급: {' '.join(M.EVAL_GRADES)}")
    eval_df = pd.DataFrame([{"연도": ev["year"], "성과": ev["performance"], "역량": ev["competency"]}
                            for ev in M.normalize_evaluations(e["evaluations"])], columns=["연도", "성과", "역량"])
    eval_df["연도"] = eval_df["연도"].astype("Int64")
    eval_edit = st.data_editor(eval_df, num_rows="dynamic", hide_index=True, width="stretch", key=k + "evals",
                               column_config={
                                   "연도": st.column_config.NumberColumn("연도", min_value=1990, max_value=today.year, step=1, format="%d"),
                                   "성과": st.column_config.SelectboxColumn("성과", options=M.EVAL_GRADES),
                                   "역량": st.column_config.SelectboxColumn("역량", options=M.EVAL_GRADES)})

    real_name_ok = True
    if csv_io.looks_like_real_name(name):
        st.warning(f"\"{name}\"은(는) 실명으로 보입니다. 실제 개인정보는 사용하지 마세요.")
        real_name_ok = st.checkbox("실명이 아닌 가상 이름임을 확인했습니다", key=k + "realname_ok")

    if not st.button("수정 저장" if existing else "추가", type="primary", key=k + "submit"):
        return

    errors = []
    name = name.strip()
    emp_no = (e["id"] if existing else emp_no).strip()
    if not existing:
        if not emp_no:
            errors.append("사원번호를 입력하세요.")
        elif not re.fullmatch(M.EMPLOYEE_NO_PATTERN, emp_no):
            errors.append("사원번호는 영문·숫자·하이픈 20자 이내로 입력하세요.")
        elif find_employee(load_state()[0], emp_no):
            errors.append(f"사원번호 {emp_no}는 이미 등록되어 있습니다.")
    if not name:
        errors.append("이름을 입력하세요.")
    if not current:
        errors.append("현재 직무를 선택하세요.")
    hist = []
    for i, row in enumerate(hist_edit.to_dict("records")):
        label = f"직무 이력 {i + 1}번째: "
        if not row.get("직무") or (isinstance(row.get("직무"), float) and pd.isna(row["직무"])):
            if any(_iso(row.get(c)) for c in ("시작일", "종료일")):
                errors.append(label + "직무를 선택하세요.")
            continue
        h = {"job": row["직무"], "startDate": _iso(row.get("시작일")), "endDate": _iso(row.get("종료일")),
             "isCurrent": bool(row.get("현재")) if not pd.isna(row.get("현재")) else False}
        if h["isCurrent"] and h["endDate"]:
            errors.append(label + "현재 직무는 종료일을 비워 두세요.")
        if h["startDate"] and h["endDate"] and h["startDate"] > h["endDate"]:
            errors.append(label + "시작일이 종료일보다 늦습니다.")
        if csv_io.is_future_date(h["startDate"]) or csv_io.is_future_date(h["endDate"]):
            errors.append(label + "오늘 이후 날짜는 쓸 수 없습니다.")
        hist.append(h)
    currents = [h for h in hist if h["isCurrent"]]
    if len(currents) > 1:
        errors.append("\"현재\"로 표시한 직무 이력은 1건만 가능합니다.")
    if len(currents) == 1 and current and currents[0]["job"] != current:
        errors.append(f"\"현재\"로 표시한 직무 이력({currents[0]['job']})과 현재 직무({current})가 다릅니다.")

    evals, seen = [], set()
    for i, row in enumerate(eval_edit.to_dict("records")):
        label = f"인사평가 {i + 1}번째: "
        year = row.get("연도")
        perf = row.get("성과") if isinstance(row.get("성과"), str) else None
        comp = row.get("역량") if isinstance(row.get("역량"), str) else None
        if (year is None or pd.isna(year)) and not perf and not comp:
            continue  # 빈 줄
        if year is None or pd.isna(year) or not (1990 <= int(year) <= today.year):
            errors.append(f"{label}연도를 1990 ~ {today.year} 사이로 입력하세요.")
            continue
        year = int(year)
        if year in seen:
            errors.append(f"{label}{year}년 평가가 두 번 있습니다.")
            continue
        if not perf and not comp:
            errors.append(f"{label}성과나 역량 등급을 하나 이상 고르세요.")
            continue
        seen.add(year)
        evals.append({"year": year, "performance": perf, "competency": comp})
    if not real_name_ok:
        errors.append("실명이 아닌 가상 이름인지 확인해 주세요.")
    if errors:
        st.error("\n".join("- " + md(x) for x in errors))
        return

    desired = {t: desired_inputs[t] for t in M.TERMS}
    placed = M.all_desired_jobs(desired)
    desired["unspecified"] = [j for j in d["unspecified"] if j not in placed]  # 시점을 지정한 직무는 미지정 목록에서 뺀다
    new_values = {
        "name": name, **{key: v.strip() or None for key, v in org.items()}, "payGrade": pay or None,
        "payGradeYears": int(pay_years) if pay_years is not None else None, "jobTitle": title.strip() or None,
        "position": position or None, "hireDate": _iso(hire), "currentJob": current, "deptStartDate": _iso(dept_start),
        "jobHistory": hist, "desiredJobs": desired, "evaluations": M.normalize_evaluations(evals),
    }

    if not require_admin():
        st.error("관리자만 직원 정보를 바꿀 수 있습니다.")
        return

    def apply(s):
        if existing:
            target = find_employee(s, existing["id"])
            target.update(new_values)
        else:
            s["employees"].append({"id": emp_no, **new_values})
    mutate(apply, "직원 정보를 수정했습니다." if existing else "직원을 추가했습니다.")
    for key in [x for x in st.session_state.keys() if str(x).startswith(k)]:
        del st.session_state[key]
    st.rerun()


# ---------------------------------------------------------------------------
# 직무 마스터 (PRD 4.5)
# ---------------------------------------------------------------------------

def rename_job_everywhere(s: dict, old: str, new: str) -> int:
    changed = 0
    for e in s["employees"]:
        touched = False
        if e["currentJob"] == old:
            e["currentJob"], touched = new, True
        d = M.normalize_desired(e["desiredJobs"])
        for t in M.TERMS + ["unspecified"]:
            if old in d[t]:
                d[t] = [new if x == old else x for x in d[t]]
                touched = True
        e["desiredJobs"] = d
        for h in e["jobHistory"]:
            if h["job"] == old:
                h["job"], touched = new, True
        changed += touched
    for j in s["jobs"]:
        j["relatedJobs"] = [new if x == old else x for x in j["relatedJobs"]]
    for r in s["reviews"]:
        if r["targetJob"] == old:
            r["targetJob"] = new
    for q in s.get("userQueries", {}).values():
        if q["targetJob"] == old:
            q["targetJob"] = new
    return changed


def job_name_error(name: str, others: list[str]) -> str | None:
    if not name:
        return "직무명을 입력하세요."
    bad = csv_io.forbidden_job_chars(name)
    if bad:
        return f"직무명에 {' '.join(bad)} 기호는 쓸 수 없습니다 (CSV 구분 기호)."
    if name in others:
        return f"'{name}' 직무는 이미 있습니다."
    return None


def add_job():
    if not require_admin():
        return
    name = st.session_state.get("new_job_name", "").strip()
    state, _, _ = load_state()
    err = job_name_error(name, job_names(state))
    if err:
        st.session_state["_flash"] = ("error", err)
        return
    mutate(lambda s: s["jobs"].append({"name": name, "relatedJobs": []}), f"'{name}' 직무를 추가했습니다. 아래에서 유관직무를 지정하세요.")
    st.session_state["new_job_name"] = ""


def save_job(old: str):
    if not require_admin():
        return
    new = st.session_state[f"job_name::{old}"].strip()
    related = st.session_state[f"job_related::{old}"]
    state, _, _ = load_state()
    err = job_name_error(new, [n for n in job_names(state) if n != old])
    if err:
        st.session_state["_flash"] = ("error", err)
        return
    result = {"renamed": 0}

    def apply(s):
        job = next(j for j in s["jobs"] if j["name"] == old)
        if new != old:
            result["renamed"] = rename_job_everywhere(s, old, new)
        job["name"], job["relatedJobs"] = new, [r for r in related if r != new]
    mutate(apply)
    st.toast("직무를 저장했습니다." + (f" 직원 {result['renamed']}명의 직무명도 함께 바꿨습니다." if result["renamed"] else ""))
    st.session_state["job_edit_select"] = new
    reset_search_widgets()


def delete_job(name: str):
    if not require_admin():
        return
    def apply(s):
        s["jobs"] = [j for j in s["jobs"] if j["name"] != name]
        for j in s["jobs"]:
            j["relatedJobs"] = [r for r in j["relatedJobs"] if r != name]
    mutate(apply, f"'{name}' 직무를 삭제했습니다.")
    st.session_state.pop("job_edit_select", None)


def render_jobs(state: dict) -> None:
    st.caption("유관직무는 이 직무로 전환배치할 때 경력으로 인정할 직무입니다. 담당자가 직접 지정하며, 도구가 자동으로 추론하지 않습니다.")
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    c1.text_input("새 직무명", max_chars=30, key="new_job_name")
    c2.button("직무 추가", type="primary", on_click=add_job, width="stretch")
    if not state["jobs"]:
        st.info("등록된 직무가 없습니다.")
        return
    st.dataframe(pd.DataFrame([{"직무명": j["name"], "유관직무": ", ".join(j["relatedJobs"]) or "지정 없음",
                                "사용 중인 직원": f"{job_usage_count(state, j['name'])}명"} for j in state["jobs"]]),
                 hide_index=True, width="stretch")

    names = job_names(state)
    if st.session_state.get("job_edit_select") not in names:
        st.session_state.pop("job_edit_select", None)
    selected = st.selectbox("수정할 직무", names, key="job_edit_select")
    job = next(j for j in state["jobs"] if j["name"] == selected)
    with st.container(border=True):
        st.text_input("직무명", value=job["name"], max_chars=30, key=f"job_name::{selected}")
        others = [n for n in names if n != selected]
        st.multiselect(f"유관직무: '{selected}' 직무로 옮길 때 경력으로 인정할 직무", others,
                       default=[r for r in job["relatedJobs"] if r in others], placeholder="지정 없음", key=f"job_related::{selected}")
        c1, c2, _ = st.columns([1, 1, 3])
        c1.button("저장", type="primary", on_click=save_job, args=(selected,), width="stretch", key=f"job_save::{selected}")
        with c2.popover("삭제", width="stretch"):
            used = job_usage_count(state, selected)
            st.warning(f"'{selected}' 직무를 쓰는 직원이 {used}명 있습니다. 직무 마스터에서 지워도 직원 데이터의 직무명은 그대로 남지만, "
                       "검색 목록과 유관직무 지정에서는 빠집니다." if used else f"'{selected}' 직무를 삭제합니다.")
            st.button("삭제하기", type="primary", on_click=delete_job, args=(selected,), key=f"job_del::{selected}")


# ---------------------------------------------------------------------------
# 분석 기준 설정 (PRD 4.6) + 데이터 관리
# ---------------------------------------------------------------------------

def restore_backup(new_state: dict):
    if not require_admin():
        return
    storage.save(new_state)
    st.toast("백업 파일로 복원했습니다.")
    st.session_state["restore_n"] = st.session_state.get("restore_n", 0) + 1
    reset_search_widgets()


def reset_all():
    if not require_admin():
        return
    storage.save(storage.empty_state())
    st.toast("전체 초기화했습니다.")
    st.session_state["reset_agree"] = False
    reset_search_widgets()


def render_settings(state: dict) -> None:
    s = state["settings"]
    w = A.term_weights_of(s)
    with st.form("settings_form"):
        c1, c2 = st.columns(2)
        tenure = c1.number_input("현부서 근속 기준 (년)", min_value=0.0, max_value=40.0, step=0.5, value=float(s["deptTenureYears"]))
        career = c2.number_input("유관경력 최소 기간 (년)", min_value=0.0, max_value=40.0, step=0.5, value=float(s["relatedCareerMinYears"]))
        st.markdown("**희망직무 시점 가중치**")
        st.caption("추천 점수 = 희망직무(아래 가중치) + 유관 업무경력 1.0 + 현부서 근속 1.0. 같은 직무를 여러 시점에 희망하면 가장 높은 가중치를 씁니다. "
                   "0 ~ 1 사이, 단기 ≥ 중기 ≥ 장기.")
        wc = st.columns(3)
        weights = {t: wc[i].number_input(M.TERM_LABELS[t], min_value=0.0, max_value=1.0, step=0.1, value=float(w[t]), key=f"w_{t}")
                   for i, t in enumerate(M.TERMS)}
        st.markdown("**필수 조건**")
        st.caption("필수 조건으로 지정한 근거를 '미충족'한 직원은 후보에서 뺍니다. 데이터가 없어 '확인 불가'인 경우에는 빼지 않고 '확인 필요'로 표시합니다.")
        rc = st.columns(3)
        required = {cid: rc[i].checkbox(A.CRITERIA_LABELS[cid], value=s["requiredCriteria"].get(cid, False), key=f"req_{cid}")
                    for i, cid in enumerate(("R1", "R2", "R3"))}
        st.caption("인사평가는 분석 기준으로 쓰지 않습니다 (상세보기 참고용).")
        if st.form_submit_button("기준 저장", type="primary") and require_admin():
            weights = {t: round(v, 2) for t, v in weights.items()}
            if not storage.valid_term_weights(weights):
                st.error("희망 시점 가중치는 0 ~ 1 사이 숫자이고, 단기 ≥ 중기 ≥ 장기 순서여야 합니다.")
            else:
                def apply(st_):
                    st_["settings"].update(deptTenureYears=tenure, relatedCareerMinYears=career, termWeights=weights, requiredCriteria=required)
                mutate(apply, "분석 기준을 저장했습니다. 후보자 탐색 결과에 바로 반영됩니다.")
                st.rerun()

    st.markdown("#### 데이터 관리")
    st.caption(f"저장 위치: 서버의 `{storage.DATA_FILE.name}` 파일. Streamlit Cloud에서는 앱이 다시 시작되면 지워질 수 있고, "
               "앱을 여는 모든 사람이 같은 데이터를 봅니다. 정기적으로 백업 파일을 받아 두세요.")
    c1, c2 = st.columns(2)
    with c1:
        st.download_button("백업 파일 받기 (JSON)", storage.backup_bytes(state), f"전환배치도구_백업_{file_stamp()}.json",
                           "application/json", on_click=record_backup, width="stretch", key="set_backup")
        st.caption("계정·비밀번호 정보는 백업 파일에 들어가지 않습니다. HTML 버전 백업은 복원할 수 있지만, 이 백업은 HTML 버전에서 열 수 없습니다(로그인용 소속 구조가 추가됨).")
    with c2:
        up = st.file_uploader("백업 파일로 복원", type=["json"], key=f"restore_{st.session_state.get('restore_n', 0)}")
        if up:
            new_state, err = storage.parse_backup(up.getvalue())
            if err:
                st.error("복원하지 못했습니다: " + err)
            else:
                st.warning(f"현재 데이터를 백업 파일({up.name}) 내용으로 덮어씁니다. 직원 {len(new_state['employees'])}명, 직무 {len(new_state['jobs'])}개.")
                st.button("복원하기", type="primary", on_click=restore_backup, args=(new_state,))

    with st.container(border=True):
        st.markdown(":red[**전체 초기화**]")
        agree = st.checkbox("직원, 직무, 검토 기록, 설정을 모두 지운다는 것을 이해했습니다. (필요하면 먼저 백업 파일을 받으세요)", key="reset_agree")
        with st.popover("전체 초기화", disabled=not agree):
            st.error("되돌릴 수 없습니다. 정말 전체 초기화할까요?")
            st.button("초기화하기", type="primary", on_click=reset_all, key="reset_confirm")


# ---------------------------------------------------------------------------
# 사용자 관리 (관리자)
# ---------------------------------------------------------------------------

def admin_action(action: str, emp_no: str):
    if not require_admin():
        return
    me = st.session_state.get("auth_user")
    if action == "reset":
        auth.reset_password(emp_no)
        st.toast(f"{emp_no}의 비밀번호를 사원번호로 초기화했습니다. 다음 로그인 때 새 비밀번호를 정하게 됩니다.")
    elif action == "unlock":
        auth.unlock(emp_no)
        st.toast(f"{emp_no}의 잠금을 풀었습니다.")
    elif action == "grant":
        auth.set_role(emp_no, True)
        st.toast(f"{emp_no}에게 관리자 권한을 주었습니다.")
    elif action == "revoke":
        if emp_no == me:
            st.session_state["_flash"] = ("error", "자기 자신의 관리자 권한은 해제할 수 없습니다.")
            return
        auth.set_role(emp_no, False)
        st.toast(f"{emp_no}의 관리자 권한을 해제했습니다.")


def render_users(state: dict, user: dict) -> None:
    cfg = auth_config()
    data = auth.load()
    emp_by_id = {e["id"]: e for e in state["employees"]}
    granted = [uid for uid, rec in data["users"].items() if rec.get("role") == "admin"]
    ids = list(emp_by_id) + [x for x in cfg["admin_ids"] + granted if x not in emp_by_id]
    ids = list(dict.fromkeys(ids))

    st.caption("아이디는 사원번호이고, 처음 비밀번호도 사원번호입니다. "
               + ("첫 로그인 때 새 비밀번호로 바꾸게 합니다. " if cfg["require_change"] else "")
               + "일반 사용자는 자기 사업부(비어 있으면 본부) 소속원만 조회하고 검토를 입력합니다.")
    st.caption("배포 설정(Secrets)에 등록된 관리자는 앱에서 해제할 수 없습니다. 직원 목록에서 삭제된 사원번호는 로그인할 수 없습니다(관리자 제외).")

    def role_of(uid):
        if uid in cfg["admin_ids"]:
            return "관리자 (배포 설정)"
        return "관리자 (앱에서 부여)" if data["users"].get(uid, {}).get("role") == "admin" else "일반"

    rows = []
    for uid in ids:
        e = emp_by_id.get(uid)
        rec = data["users"].get(uid, {})
        admin = auth.is_admin(uid, data, cfg["admin_ids"])
        scope = M.scope_of(e)
        rows.append({"사원번호": uid, "이름": e["name"] if e else "(직원 목록에 없음)",
                     "소속": (M.org_path(e) or "데이터 없음") if e else "-",
                     "권한": role_of(uid),
                     "조회 범위": "전체" if admin else (scope["label"] if scope else "없음 (소속 정보 없음)"),
                     "비밀번호": "처음 비밀번호(사원번호)" if auth.uses_initial_password(uid, data) else "변경함",
                     "상태": "잠김" if auth.is_locked(uid, data) else "정상",
                     "마지막 로그인": fmt_dt(rec.get("lastLoginAt")) or "-"})
    keyword = st.text_input("사용자 검색", placeholder="사원번호, 이름, 소속으로 찾기", key="user_filter").strip().lower()
    shown = [r for r in rows if not keyword or any(keyword in str(v).lower() for v in r.values())]
    st.dataframe(pd.DataFrame(shown), hide_index=True, width="stretch")
    if not shown:
        return

    sel = st.selectbox("관리할 사용자", [r["사원번호"] for r in shown],
                       format_func=lambda uid: f"{uid} · {emp_by_id[uid]['name'] if uid in emp_by_id else '(직원 목록에 없음)'}",
                       key="user_select")
    is_cfg_admin = sel in cfg["admin_ids"]
    is_granted = data["users"].get(sel, {}).get("role") == "admin"
    c1, c2, c3, _ = st.columns([1.2, 1, 1.4, 2])
    with c1.popover("비밀번호 초기화", width="stretch"):
        st.caption(f"{sel}의 비밀번호를 사원번호로 되돌리고 잠금을 풉니다.")
        st.button("초기화하기", type="primary", on_click=admin_action, args=("reset", sel), key=f"u_reset::{sel}")
    c2.button("잠금 해제", on_click=admin_action, args=("unlock", sel), disabled=not auth.is_locked(sel, data),
              width="stretch", key=f"u_unlock::{sel}")
    if is_cfg_admin:
        c3.button("관리자 (배포 설정)", disabled=True, width="stretch", key=f"u_role::{sel}")
    elif is_granted:
        c3.button("관리자 권한 해제", on_click=admin_action, args=("revoke", sel), disabled=sel == user["id"],
                  width="stretch", key=f"u_role::{sel}")
    else:
        with c3.popover("관리자 권한 주기", width="stretch"):
            st.warning("관리자는 모든 직원 데이터를 보고, 직원 데이터 업로드·설정 변경·백업을 할 수 있습니다.")
            st.button("권한 주기", type="primary", on_click=admin_action, args=("grant", sel), key=f"u_grant::{sel}")


# ---------------------------------------------------------------------------
# 시작
# ---------------------------------------------------------------------------

def main() -> None:
    state, warning, block = load_state()
    user = current_user(state)
    if not user:
        if st.session_state.pop("auth_user", None):
            st.session_state["_flash"] = ("warning", "계정을 더 이상 쓸 수 없어 로그아웃했습니다. 관리자에게 문의하세요.")
        flash = st.session_state.pop("_flash", None)
        if flash:
            getattr(st, flash[0])(flash[1])
        render_login(state)
        return
    if st.session_state.get("must_change"):
        render_forced_password_change(user)
        return

    render_sidebar(state, user)
    st.title("전환배치 후보자 탐색")
    # 안내 문구는 항상 같은 상자 안에 넣는다. 탭 위의 요소 개수가 바뀌면 Streamlit이 탭을 새로 만들어
    # 보고 있던 탭이 첫 번째 탭으로 돌아가기 때문이다.
    with st.container():
        render_notices(state, warning, block, user)
        if not state["employees"] and not state["jobs"]:
            if user["is_admin"]:
                st.info("아직 등록된 데이터가 없습니다. 기능을 확인하려면 가상 샘플 데이터를 불러오세요. "
                        "샘플 데이터는 실제 인물과 관계없는 가상 데이터입니다 (직원 30명, 직무 8개). 실제 업무 데이터는 직원 관리 탭에서 CSV로 가져올 수 있습니다.")
                st.button("가상 샘플 데이터 불러오기", type="primary", on_click=load_sample, key="sample_onboarding")
            else:
                st.info("아직 등록된 데이터가 없습니다. 관리자에게 문의하세요.")

    if not user["is_admin"]:
        render_search(state, user)
        return
    tabs = st.tabs(["후보자 탐색", "직원 관리", "직무 마스터", "분석 기준 설정", "사용자 관리"])
    with tabs[0]:
        render_search(state, user)
    with tabs[1]:
        render_employees(state)
    with tabs[2]:
        render_jobs(state)
    with tabs[3]:
        render_settings(state)
    with tabs[4]:
        render_users(state, user)


main()
