#!/usr/bin/env python3
"""user-admin — query and manage Firebase Auth users + users/{uid} profiles.

Stdlib only: auth comes from `gcloud auth print-access-token`, everything
else is plain REST (Identity Toolkit v1 + Firestore v1). No pip installs.

Usage:
    main.py count                          # total signed-up auth users
    main.py list [--limit N] [--offset N]  # auth users, newest first
    main.py get <uid-or-email>             # auth record + users/{uid} doc
    main.py set <uid> field=value [...]    # patch users/{uid} Firestore doc
    main.py disable <uid> | enable <uid>   # toggle the auth account

    --project overrides the project id (else GCP_PROJECT env, else
    GCP_PROJECT from .env.prod at the repo root).

`set` value coercion: true/false → bool, null → null, int/float strings →
numbers, everything else → string. Fields written by the frontend
(auth.js upsertProfile): uid, email, phoneNumber, displayName, photoURL,
provider, lastSeenAt, createdAt.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

IDTK = "https://identitytoolkit.googleapis.com/v1"
FS = "https://firestore.googleapis.com/v1"
REPO_ROOT = Path(__file__).resolve().parents[2]


def project_id(cli: str | None) -> str:
    if cli:
        return cli
    if os.environ.get("GCP_PROJECT"):
        return os.environ["GCP_PROJECT"]
    env_prod = REPO_ROOT / ".env.prod"
    if env_prod.exists():
        for line in env_prod.read_text().splitlines():
            if line.startswith("GCP_PROJECT="):
                return line.split("=", 1)[1].strip()
    sys.exit("!! No project id: pass --project, or set GCP_PROJECT (env or .env.prod)")


def token() -> str:
    try:
        out = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"!! gcloud auth print-access-token failed — run `gcloud auth login` ({e})")
    return out.stdout.strip()


def call(method: str, url: str, proj: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token()}")
    req.add_header("x-goog-user-project", proj)  # ADC user creds need a quota project
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        sys.exit(f"!! {method} {url.split('?')[0]} → HTTP {e.code}: {detail}")


# ── Auth (Identity Toolkit) ─────────────────────────────────────────


def auth_query(proj: str, limit: int = 100, offset: int = 0, count_only: bool = False) -> dict:
    body: dict = {"returnUserInfo": not count_only}
    if not count_only:
        body |= {"limit": str(limit), "offset": str(offset),
                 "sortBy": "CREATED_AT", "order": "DESC"}
    return call("POST", f"{IDTK}/projects/{proj}:queryAccounts", proj, body)


def auth_lookup(proj: str, ident: str) -> dict | None:
    key = "email" if "@" in ident else "localId"
    resp = call("POST", f"{IDTK}/projects/{proj}/accounts:lookup", proj, {key: [ident]})
    users = resp.get("users") or []
    return users[0] if users else None


def auth_set_disabled(proj: str, uid: str, disabled: bool) -> dict:
    return call("POST", f"{IDTK}/projects/{proj}/accounts:update", proj,
                {"localId": uid, "disableUser": disabled})


def fmt_user(u: dict) -> str:
    ms = u.get("createdAt")
    provider = ",".join(p.get("providerId", "?") for p in u.get("providerUserInfo", [])) or "password"
    flags = " DISABLED" if u.get("disabled") else ""
    ident = u.get("email") or u.get("phoneNumber") or "-"
    return f"{u['localId']}  {ident:<32} {provider:<12} created={_day(ms)} lastLogin={_day(u.get('lastLoginAt'))}{flags}"


def _day(ms: str | None) -> str:
    if not ms:
        return "-"
    import datetime
    return datetime.datetime.fromtimestamp(int(ms) / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")


# ── Firestore users/{uid} ───────────────────────────────────────────


def fs_doc_url(proj: str, uid: str) -> str:
    return f"{FS}/projects/{proj}/databases/(default)/documents/users/{urllib.parse.quote(uid)}"


def fs_get_profile(proj: str, uid: str) -> dict | None:
    req = urllib.request.Request(fs_doc_url(proj, uid))
    req.add_header("Authorization", f"Bearer {token()}")
    req.add_header("x-goog-user-project", proj)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def to_fs_value(raw: str) -> dict:
    if raw == "true":
        return {"booleanValue": True}
    if raw == "false":
        return {"booleanValue": False}
    if raw == "null":
        return {"nullValue": None}
    try:
        return {"integerValue": str(int(raw))}
    except ValueError:
        pass
    try:
        return {"doubleValue": float(raw)}
    except ValueError:
        pass
    return {"stringValue": raw}


def from_fs_fields(doc: dict) -> dict:
    out = {}
    for k, v in (doc.get("fields") or {}).items():
        out[k] = next(iter(v.values()))
    return out


def fs_patch(proj: str, uid: str, pairs: list[tuple[str, str]]) -> dict:
    mask = "&".join(f"updateMask.fieldPaths={urllib.parse.quote(k)}" for k, _ in pairs)
    fields = {k: to_fs_value(v) for k, v in pairs}
    return call("PATCH", f"{fs_doc_url(proj, uid)}?{mask}", proj, {"fields": fields})


# ── Commands ────────────────────────────────────────────────────────


def main() -> int:
    args = sys.argv[1:]
    proj_cli = None
    if "--project" in args:
        i = args.index("--project")
        proj_cli = args[i + 1]
        del args[i:i + 2]
    if not args:
        print(__doc__)
        return 2
    cmd, rest = args[0], args[1:]
    proj = project_id(proj_cli)

    if cmd == "count":
        print(auth_query(proj, count_only=True).get("recordsCount", "0"))

    elif cmd == "list":
        limit = int(rest[rest.index("--limit") + 1]) if "--limit" in rest else 100
        offset = int(rest[rest.index("--offset") + 1]) if "--offset" in rest else 0
        resp = auth_query(proj, limit=limit, offset=offset)
        users = resp.get("userInfo") or []
        for u in users:
            print(fmt_user(u))
        print(f"-- {len(users)} shown (recordsCount={resp.get('recordsCount', '?')})")

    elif cmd == "get":
        if not rest:
            sys.exit("usage: get <uid-or-email>")
        u = auth_lookup(proj, rest[0])
        if not u:
            sys.exit(f"!! no auth user matching {rest[0]}")
        print("── auth ──")
        redacted = {k: v for k, v in u.items() if k not in ("passwordHash", "salt")}
        print(json.dumps(redacted, indent=2))
        prof = fs_get_profile(proj, u["localId"])
        print("── users/{uid} profile ──")
        print(json.dumps(from_fs_fields(prof), indent=2, default=str) if prof
              else "(no profile doc — user never completed a frontend sign-in)")

    elif cmd == "set":
        if len(rest) < 2 or "=" not in rest[1]:
            sys.exit("usage: set <uid> field=value [field=value ...]")
        uid = rest[0]
        pairs = [tuple(p.split("=", 1)) for p in rest[1:]]
        bad = [p for p in pairs if len(p) != 2 or not p[0]]
        if bad:
            sys.exit(f"!! malformed field=value: {bad}")
        doc = fs_patch(proj, uid, pairs)
        print(json.dumps(from_fs_fields(doc), indent=2))

    elif cmd in ("disable", "enable"):
        if not rest:
            sys.exit(f"usage: {cmd} <uid>")
        auth_set_disabled(proj, rest[0], cmd == "disable")
        print(f"{rest[0]} {cmd}d")

    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
