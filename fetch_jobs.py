# -*- coding: utf-8 -*-
"""
GOREV veri motoru v1.0 (2026-07-10)
Her sabah GitHub Actions tarafindan calistirilir, data/jobs.json uretir.

Kaynaklar:
  1. hiring.cafe ic API'si (resmi degil, kirilabilir; kirilirsa site link moduna duser)
  2. Greenhouse public board API (stabil, resmi)
  3. Lever public postings API (stabil, resmi)

Kural seti burcu-on-target-search skill'inden gelir:
  - Kirmizi cizgiler otomatik PAS onerisi olarak isaretlenir (silinmez, Burcu gorur)
  - Toksik sinyal kelimeleri flag'lenir
  - Karaliste sirketleri hic listeye girmez
"""

import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ============================================================
# KONFIGURASYON — tek duzenlenecek yer burasi
# ============================================================

CONFIG = {
    # Lane -> arama sorgulari (hiring.cafe icin)
    "lanes": {
        "B": {
            "cv": "CV2_KAM",
            "queries": ["Key Account Manager", "Enterprise Account Manager",
                        "Customer Success Manager", "Partner Success Manager"],
            "keywords": ["key account", "account manager", "customer success",
                         "partner success", "client relationship", "qbr", "renewal",
                         "portfolio", "b2b"],
        },
        "C": {
            "cv": "CV4_Insights",
            "queries": ["Insights Manager", "Client Development Manager",
                        "Client Service Manager"],
            "keywords": ["insights", "client service", "client development",
                         "research", "consumer", "brand"],
        },
        "A": {
            "cv": "CV1_BD",
            "queries": ["Business Development Manager", "Partnerships Manager",
                        "Market Development Manager"],
            "keywords": ["business development", "partnership", "market entry",
                         "market development", "alliances"],
        },
        "E": {
            "cv": "CV5_Commercial_Project",
            "queries": ["Commercial Project Manager", "Launch Manager",
                        "Implementation Manager", "Onboarding Manager"],
            "keywords": ["launch", "implementation", "onboarding",
                         "commercial project", "program manager"],
        },
    },

    # Lokasyon filtresi (baslik/lokasyon metninde aranir)
    "locations_ok": ["istanbul", "turkey", "türkiye", "turkiye", "remote"],

    # KARALISTE — bu sirketler listeye hic girmez (Burcu onayli: 2026-07-10)
    "blacklist": ["trendyol", "insider"],

    # KIRMIZI CIZGI — eslesirse ilan gelir ama "PAS onerisi" olarak isaretli
    "red_flags": [
        "category manager", "merchandising", "fmcg",
        "account executive", "quota",
    ],

    # TOKSIK SINYAL — JD'de gecerse kart uzerinde uyari rozeti
    "toxic_signals": [
        "fast-paced", "fast paced", "hustle", "aggressive targets",
        "wear many hats", "work hard play hard", "high pressure",
    ],

    # YESIL SINYAL kelimeleri — kart uzerinde pozitif rozet
    "green_signals": ["jbp", "qbr", "partnership", "market entry",
                      "client relationship", "hybrid", "remote"],

    # Greenhouse board token'lari (sirket kariyer sitesi motoru)
    # Ekleme yontemi: sirketin kariyer sayfasi greenhouse.io iceriyorsa
    # URL'deki token'i buraya yaz. Ornek: "https://boards.greenhouse.io/ACME" -> "acme"
    "greenhouse_boards": [
        # "ornek-sirket-token",
    ],

    # Lever sirket adlari. Ornek: "https://jobs.lever.co/ACME" -> "acme"
    "lever_companies": [
        # "ornek-sirket",
    ],

    "hiring_cafe_days": 7,       # son N gun ilanlari
    "max_per_query": 40,
    "timeout": 25,
}

UA = {"User-Agent": "Mozilla/5.0 (job dashboard; personal use; low volume)"}
NOW = datetime.now(timezone.utc)


# ============================================================
# Yardimcilar
# ============================================================

def http_json(url, payload=None, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=CONFIG["timeout"]) as r:
        return json.loads(r.read().decode("utf-8"))


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def guess_lane(title, desc=""):
    """Baslik+aciklamadan lane tahmini ve fit skoru (0-100)."""
    text = (title + " " + desc).lower()
    best, best_hits = None, 0
    for lane, cfg in CONFIG["lanes"].items():
        hits = sum(1 for k in cfg["keywords"] if k in text)
        # Baslik eslesmesi cifte sayilir
        hits += sum(1 for q in cfg["queries"] if q.lower() in title.lower()) * 2
        if hits > best_hits:
            best, best_hits = lane, hits
    if not best:
        return None, 0
    fit = min(95, 35 + best_hits * 12)
    return best, fit


def flags_for(title, desc, company):
    text = (title + " " + desc).lower()
    f = {"red": [], "toxic": [], "green": []}
    for k in CONFIG["red_flags"]:
        if k in text:
            f["red"].append(k)
    for k in CONFIG["toxic_signals"]:
        if k in text:
            f["toxic"].append(k)
    for k in CONFIG["green_signals"]:
        if k in text:
            f["green"].append(k)
    return f


def location_ok(loc):
    l = (loc or "").lower()
    return any(x in l for x in CONFIG["locations_ok"])


def is_blacklisted(company):
    c = (company or "").lower()
    return any(b in c for b in CONFIG["blacklist"])


def make_job(title, company, location, url, posted_at, source, desc=""):
    if is_blacklisted(company):
        return None
    if not location_ok(location):
        return None
    lane, fit = guess_lane(title, desc)
    if not lane:
        return None
    return {
        "id": re.sub(r"[^a-z0-9]", "", (company + title).lower())[:60],
        "title": clean(title),
        "company": clean(company),
        "location": clean(location),
        "url": url,
        "posted_at": posted_at,
        "source": source,
        "lane": lane,
        "cv": CONFIG["lanes"][lane]["cv"],
        "fit": fit,
        "flags": flags_for(title, desc, company),
    }


# ============================================================
# Kaynak 1: hiring.cafe (resmi olmayan ic API — kirilgan)
# ============================================================

def fetch_hiring_cafe():
    jobs, errors = [], []
    endpoint = "https://hiring.cafe/api/search-jobs"
    for lane, cfg in CONFIG["lanes"].items():
        for q in cfg["queries"]:
            payload = {
                "size": CONFIG["max_per_query"],
                "page": 0,
                "searchState": {
                    "searchQuery": q,
                    "dateFetchedPastNDays": CONFIG["hiring_cafe_days"],
                    "locationSearchQuery": "Istanbul, Turkey",
                },
            }
            try:
                data = http_json(endpoint, payload)
                results = data.get("results") or data.get("jobs") or []
                for r in results:
                    info = r.get("job_information") or r
                    proc = r.get("v5_processed_job_data") or {}
                    title = info.get("title") or proc.get("job_title") or ""
                    company = (proc.get("company_name")
                               or info.get("company_name") or "")
                    loc = (proc.get("formatted_workplace_location")
                           or info.get("location") or "")
                    url = (r.get("apply_url") or info.get("apply_url")
                           or r.get("url") or "")
                    posted = (r.get("estimated_publish_date")
                              or info.get("posted_at") or "")
                    desc = clean(info.get("description") or "")[:2000]
                    j = make_job(title, company, loc, url, posted,
                                 "hiring.cafe", desc)
                    if j:
                        jobs.append(j)
            except Exception as e:
                errors.append(f"hiring.cafe [{q}]: {type(e).__name__}: {e}")
    return jobs, errors


# ============================================================
# Kaynak 2-3: Greenhouse + Lever (resmi public API'ler)
# ============================================================

def fetch_greenhouse():
    jobs, errors = [], []
    for board in CONFIG["greenhouse_boards"]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
        try:
            data = http_json(url)
            for r in data.get("jobs", []):
                j = make_job(
                    r.get("title", ""), board,
                    (r.get("location") or {}).get("name", ""),
                    r.get("absolute_url", ""),
                    r.get("updated_at", ""), "greenhouse",
                    clean(r.get("content", ""))[:2000],
                )
                if j:
                    jobs.append(j)
        except Exception as e:
            errors.append(f"greenhouse [{board}]: {type(e).__name__}")
    return jobs, errors


def fetch_lever():
    jobs, errors = [], []
    for co in CONFIG["lever_companies"]:
        url = f"https://api.lever.co/v0/postings/{co}?mode=json"
        try:
            data = http_json(url)
            for r in data:
                cat = r.get("categories") or {}
                created = r.get("createdAt")
                posted = (datetime.fromtimestamp(created / 1000, timezone.utc)
                          .isoformat() if created else "")
                j = make_job(
                    r.get("text", ""), co, cat.get("location", ""),
                    r.get("hostedUrl", ""), posted, "lever",
                    clean(r.get("descriptionPlain", ""))[:2000],
                )
                if j:
                    jobs.append(j)
        except Exception as e:
            errors.append(f"lever [{co}]: {type(e).__name__}")
    return jobs, errors


# ============================================================
# Ana akis
# ============================================================

def main():
    all_jobs, all_errors = [], []
    for fn in (fetch_hiring_cafe, fetch_greenhouse, fetch_lever):
        jobs, errors = fn()
        all_jobs.extend(jobs)
        all_errors.extend(errors)

    # Tekillestir (id bazli), fit'e gore sirala
    seen, unique = set(), []
    for j in sorted(all_jobs, key=lambda x: -x["fit"]):
        if j["id"] not in seen:
            seen.add(j["id"])
            unique.append(j)

    out = {
        "version": "1.0",
        "fetched_at": NOW.isoformat(),
        "job_count": len(unique),
        "errors": all_errors,
        "jobs": unique,
    }
    with open("data/jobs.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"OK: {len(unique)} ilan yazildi. Hata: {len(all_errors)}")
    for e in all_errors:
        print("  !", e)


if __name__ == "__main__":
    main()
