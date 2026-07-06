#!/usr/bin/env python3
"""One-time: link the Firebase Hosting site to robotics.arboryx.ai.

Registers the custom domain with Firebase Hosting, reads back the DNS
records Firebase requires (A/AAAA + a TXT ownership record), and creates
them in the Cloudflare zone via the Cloudflare API. Then polls until
Firebase reports the domain active.

Idempotent — safe to re-run (e.g. to re-check provisioning status).

Reads everything from .env.prod:
    GCP_PROJECT          Firebase project id
    FIREBASE_DEPLOY_KEY  service-account key JSON (auths the Firebase API)
    CUSTOM_DOMAIN        e.g. robotics.arboryx.ai
    CF_ZONE_NAME         the Cloudflare zone, e.g. arboryx.ai
    CF_API_TOKEN         Cloudflare API token — DNS:Edit on that zone
    HOSTING_SITE         (optional) Hosting site id; defaults to GCP_PROJECT

Run:  make link-domain     (or: python3 dev-utils/link_domain.py)
"""
from __future__ import annotations

import os
import sys
import time

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

FB_API = "https://firebasehosting.googleapis.com/v1beta1"
CF_API = "https://api.cloudflare.com/client/v4"
# DNS record types this script can push to Cloudflare via `content`.
SIMPLE_TYPES = {"A", "AAAA", "TXT", "CNAME"}


def die(msg: str) -> None:
    print(f"!! {msg}")
    sys.exit(1)


# ── .env.prod ───────────────────────────────────────────────────────
def load_env(path: str) -> dict[str, str]:
    if not os.path.isfile(path):
        die(f"{path} missing — cp .env.prod.example .env.prod and fill in")
    env: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ── Firebase Hosting customDomains API ──────────────────────────────
def google_token(key_file: str) -> str:
    if not os.path.isfile(key_file):
        die(f"FIREBASE_DEPLOY_KEY points at a missing file: {key_file}\n"
            "   run `make firebase-sa` first")
    creds = service_account.Credentials.from_service_account_file(
        key_file, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(GoogleAuthRequest())
    return creds.token


def fb_get(parent: str, domain: str, token: str) -> dict | None:
    r = requests.get(
        f"{FB_API}/{parent}/customDomains/{domain}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if r.status_code == 404:
        return None
    if not r.ok:
        die(f"Firebase get customDomain failed [{r.status_code}]: {r.text}")
    return r.json()


def fb_create(parent: str, domain: str, token: str) -> None:
    r = requests.post(
        f"{FB_API}/{parent}/customDomains",
        params={"customDomainId": domain},
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={},
        timeout=30,
    )
    if r.status_code == 409:
        print(f"   customDomain {domain} already registered — reusing")
        return
    if not r.ok:
        die(f"Firebase create customDomain failed [{r.status_code}]: {r.text}")
    print(f"   registered customDomain {domain}")


def desired_records(cd: dict) -> list[dict]:
    """Flatten requiredDnsUpdates.desired into [{name,type,rdata}, ...]."""
    out: list[dict] = []
    for rset in (cd.get("requiredDnsUpdates") or {}).get("desired", []):
        for rec in rset.get("records", []):
            out.append({
                "name": (rec.get("domainName") or rset.get("domainName", "")).rstrip("."),
                "type": rec.get("type", ""),
                "rdata": rec.get("rdata", ""),
            })
    return out


# ── Cloudflare DNS API ──────────────────────────────────────────────
def cf(method: str, path: str, token: str, **kw) -> dict:
    r = requests.request(
        method, f"{CF_API}{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        timeout=30, **kw,
    )
    body = r.json() if r.content else {}
    if not r.ok or not body.get("success", False):
        errs = body.get("errors", r.text)
        die(f"Cloudflare {method} {path} failed [{r.status_code}]: {errs}")
    return body


def cf_zone_id(zone_name: str, token: str) -> str:
    body = cf("GET", f"/zones?name={zone_name}", token)
    res = body.get("result", [])
    if not res:
        die(f"Cloudflare zone '{zone_name}' not found (or token lacks access)")
    return res[0]["id"]


def cf_upsert(zone_id: str, token: str, name: str, rtype: str, content: str) -> None:
    # TXT values sometimes arrive quoted — Cloudflare stores the raw string.
    if rtype == "TXT":
        content = content.strip().strip('"')
    existing = cf("GET", f"/zones/{zone_id}/dns_records"
                  f"?type={rtype}&name={name}", token).get("result", [])
    payload = {"type": rtype, "name": name, "content": content,
               "ttl": 1, "proxied": False}
    match = next((e for e in existing if e.get("content") == content), None)
    if match:
        print(f"   = {rtype:5} {name} — already set")
        return
    if existing:
        # Same name+type, different value — update the first one in place.
        cf("PUT", f"/zones/{zone_id}/dns_records/{existing[0]['id']}",
           token, json=payload)
        print(f"   ~ {rtype:5} {name} — updated")
    else:
        cf("POST", f"/zones/{zone_id}/dns_records", token, json=payload)
        print(f"   + {rtype:5} {name} — created")


# ── Main ────────────────────────────────────────────────────────────
def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    env = load_env(".env.prod")

    project = env.get("GCP_PROJECT") or die("set GCP_PROJECT in .env.prod")
    domain = env.get("CUSTOM_DOMAIN") or die("set CUSTOM_DOMAIN in .env.prod")
    zone_name = env.get("CF_ZONE_NAME") or die("set CF_ZONE_NAME in .env.prod")
    cf_token = env.get("CF_API_TOKEN") or die(
        "set CF_API_TOKEN in .env.prod — create one at\n"
        "   Cloudflare → My Profile → API Tokens → Create Token →\n"
        "   'Edit zone DNS' template, scoped to the arboryx.ai zone")
    key_file = env.get("FIREBASE_DEPLOY_KEY") or die(
        "set FIREBASE_DEPLOY_KEY in .env.prod — run `make firebase-sa`")
    site = env.get("HOSTING_SITE") or project
    parent = f"projects/{project}/sites/{site}"

    print(f"Linking {domain} → Firebase Hosting site '{site}'")

    # 1. Register the custom domain with Firebase Hosting.
    print("── 1/4  Firebase: register custom domain ─────────────────────")
    token = google_token(key_file)
    fb_create(parent, domain, token)

    # 2. Poll until Firebase has computed the required DNS records.
    print("── 2/4  Firebase: read required DNS records ──────────────────")
    records: list[dict] = []
    for _ in range(15):
        cd = fb_get(parent, domain, token) or {}
        records = desired_records(cd)
        if records:
            break
        time.sleep(5)
    if not records:
        die("Firebase did not return any required DNS records — re-run "
            "shortly (it can lag a few minutes after registration).")
    for r in records:
        print(f"   need {r['type']:5} {r['name']}  {r['rdata']}")

    # 3. Create the records in Cloudflare.
    print("── 3/4  Cloudflare: upsert DNS records ───────────────────────")
    zone_id = cf_zone_id(zone_name, cf_token)
    deferred: list[dict] = []
    for r in records:
        if r["type"] in SIMPLE_TYPES:
            cf_upsert(zone_id, cf_token, r["name"], r["type"], r["rdata"])
        else:
            deferred.append(r)
    for r in deferred:
        print(f"   !! add this {r['type']} record by hand in Cloudflare:")
        print(f"      {r['name']}  {r['type']}  {r['rdata']}")

    # 4. Poll for activation (cert issuance can lag — don't block forever).
    print("── 4/4  Firebase: wait for activation ────────────────────────")
    host = own = cert = "?"
    for _ in range(20):
        cd = fb_get(parent, domain, token) or {}
        host = cd.get("hostState", "?")
        own = cd.get("ownershipState", "?")
        cert = (cd.get("cert") or {}).get("state", "?")
        print(f"   host={host}  ownership={own}  cert={cert}")
        if host == "HOST_ACTIVE" and cert == "CERT_ACTIVE":
            print(f"\n✅ {domain} is live: https://{domain}")
            return 0
        time.sleep(15)

    print("")
    print(f"DNS records are in place. Firebase is still provisioning "
          f"({domain}):")
    print(f"   host={host}  ownership={own}  cert={cert}")
    print("Certificate issuance can take up to ~24h. Re-run "
          "`make link-domain` later to re-check — it's idempotent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
