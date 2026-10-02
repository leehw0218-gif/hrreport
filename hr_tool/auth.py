"""로그인과 권한 (Streamlit 버전 전용)

- 아이디는 사원번호. 처음 비밀번호도 사원번호이고, 설정(require_password_change)에 따라 첫 로그인 때 바꾸게 한다.
- 비밀번호는 PBKDF2-SHA256 + 사용자별 salt로 저장한다. 원문은 어디에도 저장하지 않는다.
- 계정 정보는 업무 데이터와 분리해 data/auth.json 에 둔다. 백업 파일에는 넣지 않는다.
- 같은 사원번호로 5번 연속 틀리면 5분 동안 로그인을 막는다.
- 관리자: 배포 설정(Secrets)의 admin_ids + 앱 안에서 관리자가 부여한 사람.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time

from . import model as M
from . import storage

AUTH_FILE = storage.DATA_DIR / "auth.json"
ITERATIONS = 200_000
MAX_FAILS = 5
LOCK_SECONDS = 300
MIN_PASSWORD_LENGTH = 8


def _hash(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), ITERATIONS).hex()


def load() -> dict:
    if not AUTH_FILE.exists():
        return {"users": {}}
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # 계정 파일이 깨지면 모두 처음 비밀번호(사원번호)로 돌아간다. 원본은 지우지 않고 보관한다
        AUTH_FILE.replace(AUTH_FILE.with_name(f"auth.corrupt-{M.now_kst():%Y%m%d%H%M%S}.json"))
        return {"users": {}}
    return data if isinstance(data.get("users"), dict) else {"users": {}}


def save(data: dict) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=AUTH_FILE.parent, prefix=".auth.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, AUTH_FILE)


def _record(data: dict, emp_no: str) -> dict:
    return data["users"].setdefault(emp_no, {})


# ---------------------------------------------------------------------------
# 권한
# ---------------------------------------------------------------------------

def is_admin(emp_no: str, data: dict, config_admins: list[str]) -> bool:
    return emp_no in config_admins or data["users"].get(emp_no, {}).get("role") == "admin"


def account_exists(emp_no: str, employee_ids: set[str], data: dict, config_admins: list[str]) -> bool:
    """직원 목록에 있거나 관리자로 지정된 사원번호만 로그인할 수 있다"""
    return emp_no in employee_ids or is_admin(emp_no, data, config_admins)


def uses_initial_password(emp_no: str, data: dict) -> bool:
    return not data["users"].get(emp_no, {}).get("hash")


# ---------------------------------------------------------------------------
# 로그인
# ---------------------------------------------------------------------------

def verify(emp_no: str, password: str, employee_ids: set[str], config_admins: list[str],
           require_change: bool) -> tuple[bool, str | None, bool]:
    """(성공 여부, 실패 이유, 비밀번호를 바꿔야 하는지)

    실패 이유는 계정이 있는지 알 수 없도록 같은 문구를 쓴다 (잠금만 따로 알린다).
    """
    generic = "사원번호 또는 비밀번호가 올바르지 않습니다."
    emp_no = emp_no.strip()
    data = load()
    if not emp_no or not account_exists(emp_no, employee_ids, data, config_admins):
        return False, generic, False
    rec = _record(data, emp_no)
    now = time.time()
    if rec.get("lockedUntil", 0) > now:
        minutes = int((rec["lockedUntil"] - now) // 60) + 1
        return False, f"비밀번호를 {MAX_FAILS}번 틀려 잠겼습니다. 약 {minutes}분 뒤에 다시 시도하거나 관리자에게 잠금 해제를 요청하세요.", False

    if rec.get("hash"):
        ok = hmac.compare_digest(_hash(password, rec["salt"]), rec["hash"])
    else:
        ok = hmac.compare_digest(password.encode("utf-8"), emp_no.encode("utf-8"))  # 처음 비밀번호 = 사원번호

    if not ok:
        rec["fails"] = rec.get("fails", 0) + 1
        if rec["fails"] >= MAX_FAILS:
            rec["lockedUntil"] = now + LOCK_SECONDS
            rec["fails"] = 0
        save(data)
        return False, generic, False

    must_change = require_change and (not rec.get("hash") or rec.get("mustChange", False))
    rec.update(fails=0, lockedUntil=0, lastLoginAt=M.now_kst().isoformat(timespec="seconds"))
    save(data)
    return True, None, must_change


def password_problem(emp_no: str, password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다."
    if password == emp_no:
        return "사원번호와 다른 비밀번호를 쓰세요."
    if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        return "영문과 숫자를 함께 쓰세요."
    return None


def set_password(emp_no: str, password: str) -> None:
    data = load()
    salt = secrets.token_hex(16)
    _record(data, emp_no).update(salt=salt, hash=_hash(password, salt), mustChange=False,
                                 passwordChangedAt=M.now_kst().isoformat(timespec="seconds"))
    save(data)


def check_password(emp_no: str, password: str) -> bool:
    """비밀번호 변경 전 현재 비밀번호 확인 (실패 횟수는 세지 않음)"""
    rec = load()["users"].get(emp_no, {})
    if rec.get("hash"):
        return hmac.compare_digest(_hash(password, rec["salt"]), rec["hash"])
    return hmac.compare_digest(password.encode("utf-8"), emp_no.encode("utf-8"))


# ---------------------------------------------------------------------------
# 관리자 기능
# ---------------------------------------------------------------------------

def reset_password(emp_no: str) -> None:
    """처음 비밀번호(사원번호)로 되돌리고 잠금을 푼다"""
    data = load()
    rec = _record(data, emp_no)
    for k in ("hash", "salt", "passwordChangedAt"):
        rec.pop(k, None)
    rec.update(mustChange=True, fails=0, lockedUntil=0)
    save(data)


def unlock(emp_no: str) -> None:
    data = load()
    _record(data, emp_no).update(fails=0, lockedUntil=0)
    save(data)


def set_role(emp_no: str, admin: bool) -> None:
    data = load()
    rec = _record(data, emp_no)
    if admin:
        rec["role"] = "admin"
    else:
        rec.pop("role", None)
    save(data)


def is_locked(emp_no: str, data: dict) -> bool:
    return data["users"].get(emp_no, {}).get("lockedUntil", 0) > time.time()
