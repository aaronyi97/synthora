"""
Auth routes: send-code, register-phone, register, login, logout, me,
change-password, delete-account.

Extracted from app.py as part of Phase 3 Q2 route split (DEV-PHASE3-ROUTES-AUTH-R1).
All behaviour is identical to the original inline implementation.

Dependency pattern: lazy-import `agoracle.api.app` inside each handler to avoid
circular imports (app.py → routes/auth.py → app.py).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agoracle.api.deps import _get_client_ip, _get_user, _get_user_id, normalize_locale, require_auth
from agoracle.api.schemas import (
    AuthMeResponse,
    AuthResponse,
    ChangePasswordResponse,
    DeleteAccountResponse,
    LogoutResponse,
    SendCodeResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=30)
    password: str = Field(..., min_length=8, max_length=100)
    display_name: str = Field("", max_length=50)


class SendCodeRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=15)
    purpose: str = Field("register", pattern="^(register|login|reset)$")


class PhoneRegisterRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=15)
    code: str = Field(..., min_length=6, max_length=6)
    password: str = Field(..., min_length=8, max_length=100)
    display_name: str = Field("", max_length=50)


class LoginRequest(BaseModel):
    username: str = Field("", max_length=100)
    password: str = Field(..., max_length=100)
    phone: str = Field("", max_length=15)


class DeleteAccountBody(BaseModel):
    password: str


class ChangePasswordBody(BaseModel):
    old_password: str = Field(..., max_length=100)
    new_password: str = Field(..., min_length=8, max_length=100)


class ApiKeyStatusResponse(BaseModel):
    status: str = "ok"
    has_api_key: bool
    auth_scheme: str = "Bearer"
    session_coupled: bool = False


class ApiKeyRotateResponse(BaseModel):
    status: str = "ok"
    api_key: str
    auth_scheme: str = "Bearer"
    session_coupled: bool = False


# ── helpers ────────────────────────────────────────────────────────────────

def _app():
    import agoracle.api.app as _m
    return _m


# ── endpoints ────────────────────────────────────────────────────────────────

@router.post("/auth/send-code", response_model=SendCodeResponse)
async def send_code(body: SendCodeRequest, request: Request):
    """Send SMS verification code. Rate-limited per phone and per IP."""
    m = _app()
    req_phone = body.phone
    req_purpose = body.purpose

    if not m.state.user_store:
        raise HTTPException(status_code=503, detail="User system not available")

    import re
    if not re.match(r'^1[3-9]\d{9}$', req_phone):
        raise HTTPException(status_code=422, detail="请输入正确的手机号")

    rate_err = await m.state.user_store.check_send_rate(req_phone, req_purpose)
    if rate_err:
        raise HTTPException(status_code=429, detail=rate_err)

    from agoracle.adapters.sms.aliyun_sms import generate_code, send_verification_code
    code = generate_code(6)
    await m.state.user_store.save_verification_code(req_phone, code, req_purpose)
    ok, msg = await send_verification_code(req_phone, code)
    if not ok:
        raise HTTPException(status_code=502, detail="短信发送失败，请稍后重试")
    return {"status": "ok", "message": "验证码已发送"}


@router.post("/auth/register-phone", response_model=AuthResponse)
async def register_phone(body: PhoneRegisterRequest, request: Request):
    """Register a new account using phone number + SMS code verification."""
    m = _app()
    req_phone = body.phone
    req_code = body.code
    req_password = body.password
    req_display_name = body.display_name

    if not m.state.user_store:
        raise HTTPException(status_code=503, detail="User system not available")

    import re
    if not re.match(r'^1[3-9]\d{9}$', req_phone):
        raise HTTPException(status_code=422, detail="请输入正确的手机号")

    valid = await m.state.user_store.verify_code(req_phone, req_code, "register")
    if not valid:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    try:
        user = await m.state.user_store.register_with_phone(
            req_phone, req_password, req_display_name
        )
    except ValueError:
        raise HTTPException(status_code=409, detail="该手机号已注册")

    # SEC-L1-02: Grant initial credits (consistent with /auth/register)
    if m.state.quota_service and user.get("id"):
        m.state.quota_service.set_user_total_credits(user["id"], 300)
        logger.info(f"[REGISTER-PHONE] user_id={user['id']} granted 300 initial credits")
    session_id = await m.state.user_store.create_session(user["id"])
    resp_body = {
        "status": "ok",
        "username": user["username"],
        "display_name": user["display_name"],
        "is_admin": user.get("is_admin", False),
    }
    response = JSONResponse(content=resp_body)
    m._set_auth_cookie(response, session_id)
    return response


@router.post("/auth/register", response_model=AuthResponse)
async def register(body: RegisterRequest, request: Request):
    m = _app()
    req_username = body.username
    req_password = body.password
    req_display_name = body.display_name

    if not m.state.user_store:
        raise HTTPException(status_code=503, detail="User system not available")
    try:
        user = await m.state.user_store.register(
            req_username, req_password, req_display_name
        )
        # v4.17: Grant 300 initial credits on registration
        if m.state.quota_service and user.get("id"):
            m.state.quota_service.set_user_total_credits(user["id"], 300)
            logger.info(f"[REGISTER] user_id={user['id']} granted 300 initial credits")
        session_id = await m.state.user_store.create_session(user["id"])
        resp_body = {
            "status": "ok",
            "username": user["username"],
            "display_name": user["display_name"],
            "is_admin": user.get("is_admin", False),
        }
        response = JSONResponse(content=resp_body)
        m._set_auth_cookie(response, session_id)
        return response
    except ValueError:
        raise HTTPException(status_code=409, detail="用户名已被注册")


@router.post("/auth/login", response_model=AuthResponse)
async def login(body: LoginRequest, request: Request):
    m = _app()
    req_username = body.username
    req_password = body.password
    req_phone = body.phone

    if not m.state.user_store:
        raise HTTPException(status_code=503, detail="User system not available")

    if not req_username and not req_phone:
        raise HTTPException(status_code=422, detail="请输入用户名或手机号")

    # SEC-005: check persistent lockout before attempting auth
    client_ip = _get_client_ip(request)
    identifier = req_phone if req_phone else req_username
    remaining = await m.state.user_store.check_login_locked(identifier, client_ip)
    if remaining is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {remaining // 60 + 1} minutes.",
            headers={"Retry-After": str(remaining)},
        )

    # Support login by phone number or username
    if req_phone:
        user = await m.state.user_store.login_by_phone(req_phone, req_password)
        identifier = req_phone
    else:
        user = await m.state.user_store.login(req_username, req_password)
        identifier = req_username

    if not user:
        await m.state.user_store.record_login_failure(identifier, client_ip)
        raise HTTPException(status_code=401, detail="手机号/用户名或密码错误")

    # Clear failures on success
    await m.state.user_store.clear_login_failures(identifier, client_ip)

    session_id = await m.state.user_store.create_session(user["id"])
    resp_body = {
        "status": "ok",
        "username": user["username"],
        "display_name": user["display_name"],
        "is_admin": user.get("is_admin", False),
    }
    response = JSONResponse(content=resp_body)
    m._set_auth_cookie(response, session_id)
    return response


@router.post("/auth/logout", response_model=LogoutResponse)
async def logout(request: Request):
    """Revoke session server-side and clear cookie (SEC-003)."""
    m = _app()
    session_id = request.cookies.get("session", "")
    if session_id and m.state.user_store:
        try:
            await m.state.user_store.revoke_session(session_id)
        except Exception as e:
            logger.warning(f"Session revoke failed: {e}")
    response = JSONResponse(content={"status": "ok"})
    response.delete_cookie(key="session", path="/")
    return response


@router.get("/auth/me", response_model=AuthMeResponse)
async def me(request: Request):
    m = _app()
    require_auth(request)
    user = _get_user(request)
    # Get query count
    count = 0
    if m.state.user_store:
        count = await m.state.user_store.get_history_count(user["id"])
    preferred_language = "zh-CN"
    if m.state.profile_store:
        try:
            profile = await m.state.profile_store.load(user["id"])
            preferred_language = normalize_locale(profile.preferred_language)
        except Exception as e:
            logger.debug(f"Failed to load preferred language for /auth/me: {e}")
    return {
        "username": user["username"],
        "display_name": user["display_name"],
        "is_admin": user["is_admin"],
        "query_count": count,
        "preferred_language": preferred_language,
    }


@router.get("/auth/api-key", response_model=ApiKeyStatusResponse)
async def api_key_status(request: Request):
    m = _app()
    uid = require_auth(request)
    if not m.state.user_store:
        raise HTTPException(status_code=503, detail="User store not available")
    has_api_key = await m.state.user_store.has_api_key(uid)
    return ApiKeyStatusResponse(has_api_key=has_api_key)


@router.post("/auth/api-key/rotate", response_model=ApiKeyRotateResponse)
async def rotate_api_key(request: Request):
    m = _app()
    uid = require_auth(request)
    if not m.state.user_store:
        raise HTTPException(status_code=503, detail="User store not available")
    new_key = await m.state.user_store.reset_api_key(uid)
    return ApiKeyRotateResponse(api_key=new_key)


@router.post("/auth/change-password", response_model=ChangePasswordResponse)
async def change_password(body: ChangePasswordBody, request: Request):
    """Change the current user's password (step-up auth: requires old password)."""
    m = _app()
    uid = require_auth(request)
    if not m.state.user_store:
        raise HTTPException(status_code=503, detail="User store not available")
    old_pw = body.old_password
    new_pw = body.new_password
    ok = await m.state.user_store.verify_password_by_id(uid, old_pw)
    if not ok:
        raise HTTPException(status_code=403, detail="旧密码验证失败")
    user_obj = _get_user(request) or {}
    username = user_obj.get("username", "")
    if not username:
        raise HTTPException(status_code=500, detail="Unable to resolve username")
    await m.state.user_store.update_password(username, new_pw)
    # update_password revokes all sessions (including current).
    # Issue a new session so the user isn't kicked out immediately.
    try:
        new_session_id = await m.state.user_store.create_session(uid)
        response = JSONResponse(content={"status": "ok", "message": "密码已更新"})
        m._set_auth_cookie(response, new_session_id)
        return response
    except Exception:
        # Session re-creation failed — password still changed, user will need to re-login
        return {"status": "ok", "message": "密码已更新，请重新登录"}


@router.delete("/auth/account", response_model=DeleteAccountResponse)
async def delete_account(request: Request, body: DeleteAccountBody, confirm: str = ""):
    """Permanently delete the current user's account and all associated data.

    Requires ?confirm=DELETE + request body {"password": "..."} (step-up auth, RC-10).
    Deletes: user record, profile JSON, query history, sessions, usage data, uploads.
    """
    m = _app()
    if confirm != "DELETE":
        raise HTTPException(
            status_code=400,
            detail="必须传入 ?confirm=DELETE 以确认删除操作（不可逆）",
        )
    uid = require_auth(request)
    password = body.password

    # RC-10: Step-up auth — verify password before irreversible deletion
    if m.state.user_store:
        ok = await m.state.user_store.verify_password_by_id(uid, password)
        if not ok:
            logger.warning(f"Step-up auth failed for delete_account: uid={uid}")
            raise HTTPException(status_code=403, detail="密码验证失败，删除操作已拒绝")

    from agoracle.config.loader import PROJECT_ROOT
    from agoracle.api.app import UPLOAD_DIR

    # Delete uploaded files owned by this user (RC-09)
    deleted_files = 0
    try:
        for owner_file in UPLOAD_DIR.glob("*.owner"):
            try:
                file_owner = int(owner_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                continue
            if file_owner != uid:
                continue
            file_id_stem = owner_file.stem  # e.g. "abc123def456"
            for sibling in UPLOAD_DIR.glob(f"{file_id_stem}.*"):
                try:
                    sibling.unlink()
                    deleted_files += 1
                except OSError as e:
                    logger.warning(f"Failed to delete upload {sibling}: {e}")
    except Exception as e:
        logger.warning(f"Upload cleanup failed for uid={uid}: {e}")

    # Delete profile JSON
    if m.state.profile_store:
        try:
            profile_file = (
                PROJECT_ROOT
                / m.state.config.memory.profile_path
                / f"user_{uid}.json"
            )
            if profile_file.exists():
                profile_file.unlink()
        except Exception as e:
            logger.warning(f"Profile deletion failed for uid={uid}: {e}")

    # Delete feedback entries for this user's queries before query_history is purged.
    if getattr(m.state, 'feedback_store', None) and m.state.user_store:
        try:
            cursor = await m.state.user_store._ensure_db().execute(
                "SELECT query_id FROM query_history WHERE user_id = ?",
                (uid,),
            )
            rows = await cursor.fetchall()
            if rows:
                qids = {r[0] for r in rows}
                await m.state.feedback_store.delete_by_query_ids(qids)
        except Exception as e:
            logger.warning(f"Feedback cleanup failed for uid={uid}: {e}")

    # Delete user record + history from SQLite
    if m.state.user_store:
        await m.state.user_store.delete_user(uid)

    # Delete quota history
    if m.state.quota_service:
        try:
            m.state.quota_service.delete_user(uid)
        except Exception:
            pass

    logger.info(f"Account permanently deleted: uid={uid}, uploads_removed={deleted_files} (原则#22)")
    return {"status": "deleted", "message": "账户及所有数据已永久删除"}
