# -*- coding: utf-8 -*-
"""
GOREV veri motoru v1.4 (2026-07-10)
Her sabah GitHub Actions tarafindan calistirilir, data/jobs.json uretir.

Kaynaklar:
  1. JSearch API (Google for Jobs verisi; JSEARCH_KEY secret gerekir, en genis kaynak)
  2. hiring.cafe ic API'si (resmi degil, kirilabilir)
  3. Remotive + Arbeitnow (remote Avrupa, anahtar gerekmez)
  4. Greenhouse/Lever (istege bagli sirket listesi)

Kural seti burcu-on-target-search skill'inden gelir:
  - Kirmizi cizgiler otomatik PAS onerisi olarak isaretlenir (silinmez, Burcu gorur)
  - Toksik sinyal kelimeleri flag'lenir
  - Karaliste sirketleri hic listeye girmez
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

# ============================================================
# KONFIGURASYON — tek duzenlenecek yer burasi
# ============================================================

CONFIG = {
    # Lane -> arama sorgulari (hiring.cafe icin)
    "lanes": {
        "B": {
            "cv": "CV2_KAM",
            "title_patterns": ["account manager", "customer success",
                               "partner success", "account executive",
                               "commercial manager", "key account"],
            "queries": ["Key Account Manager", "Enterprise Account Manager",
                        "Customer Success Manager", "Partner Success Manager"],
            "keywords": ["key account", "account manager", "customer success",
                         "partner success", "client relationship", "qbr", "renewal",
                         "portfolio", "b2b"],
        },
        "C": {
            "cv": "CV4_Insights",
            "title_patterns": ["insight", "client service", "client development",
                               "client partner", "research manager"],
            "queries": ["Insights Manager", "Client Development Manager",
                        "Client Service Manager"],
            "keywords": ["insights", "client service", "client development",
                         "research", "consumer", "brand"],
        },
        "A": {
            "cv": "CV1_BD",
            "title_patterns": ["business development", "partnership",
                               "market development", "alliances", "bd manager"],
            "queries": ["Business Development Manager", "Partnerships Manager",
                        "Market Development Manager"],
            "keywords": ["business development", "partnership", "market entry",
                         "market development", "alliances"],
        },
        "E": {
            "cv": "CV5_Commercial_Project",
            "title_patterns": ["launch manager", "implementation manager",
                               "onboarding manager", "commercial project",
                               "program manager"],
            "queries": ["Commercial Project Manager", "Launch Manager",
                        "Implementation Manager", "Onboarding Manager"],
            "keywords": ["launch", "implementation", "onboarding",
                         "commercial project", "program manager"],
        },
    },

    # Lokasyon filtresi (baslik/lokasyon metninde aranir)
    "locations_ok": ["istanbul", "turkey", "türkiye", "turkiye", "hybrid", "hibrit", "ankara", "izmir"],
    "include_remote_sources": False,   # True yapinca Remotive/Arbeitnow acilir
    "hiring_cafe_enabled": False,       # eski API kirik (2026-05), True yapma
    "max_age_days": 7,                 # son 1 hafta
    # Baslikta gecerse ilan tamamen elenir
    "title_blocklist": ["developer", "engineer", "software", "copywriter",
                        "designer", "intern", "stajyer", "architect",
                        "scientist", "accountant", "frontend", "backend",
                        "devops", "recruiter", "nurse", "teacher"],
    # Lane oncelikleri: BD ve KAM/Commercial once
    "lane_priority": {"A": 3, "B": 3, "C": 1, "E": 1, "D": 0},

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
    try:
        with urllib.request.urlopen(req, timeout=CONFIG["timeout"]) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "ignore")[:180]
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {e.code} {body}".strip()) from None


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def guess_lane(title, desc=""):
    """Lane tahmini. Kural: baslikta lane pattern'i YOKSA ilan elenir."""
    t = title.lower()
    if any(b in t for b in CONFIG["title_blocklist"]):
        return None, 0
    text = (t + " " + desc.lower())
    best, best_score = None, 0
    for lane, cfg in CONFIG["lanes"].items():
        if not any(p in t for p in cfg.get("title_patterns", [])):
            continue  # baslik eslesmesi zorunlu
        hits = sum(1 for k in cfg["keywords"] if k in text)
        score = 50 + hits * 8 + CONFIG["lane_priority"].get(lane, 0) * 3
        if score > best_score:
            best, best_score = lane, score
    if not best:
        return None, 0
    return best, min(95, best_score)


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
    # Son N gun filtresi (tarihi olan ilanlar icin)
    if posted_at:
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(str(posted_at).replace("Z", "+00:00"))
            if (NOW - ts).days > CONFIG["max_age_days"]:
                return None
        except Exception:
            pass
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
        "prio": CONFIG["lane_priority"].get(lane, 0),
        "flags": flags_for(title, desc, company),
    }




# ============================================================
# Kaynak: The Muse (resmi public API, anahtar gerekmez)
# Istanbul, Turkey konumu resmi olarak destekleniyor.
# ============================================================

def fetch_themuse():
    jobs, errors = [], []
    for loc in ["Istanbul, Turkey", "Turkey"]:
        for page in range(3):
            url = ("https://www.themuse.com/api/public/jobs?page=" + str(page)
                   + "&location=" + urllib.parse.quote(loc))
            try:
                data = http_json(url)
                results = data.get("results", [])
                if not results:
                    if page == 0:
                        errors.append(f"themuse [{loc}]: API calisti, 0 ham sonuc (o an ilan yok)")
                    break
                for r in results:
                    company = (r.get("company") or {}).get("name", "")
                    locs = r.get("locations") or []
                    loc_name = locs[0].get("name", "") if locs else loc
                    refs = r.get("refs") or {}
                    j = make_job(
                        r.get("name", ""), company, loc_name,
                        refs.get("landing_page", ""),
                        r.get("publication_date", ""), "themuse",
                        clean(r.get("contents", ""))[:2000],
                    )
                    if j:
                        jobs.append(j)
            except Exception as e:
                errors.append(f"themuse [{loc} p{page}]: {e}")
                break
    return jobs, errors


# ============================================================
# Kaynak: JSearch (Google for Jobs verisi, RapidAPI)
# JSEARCH_KEY yoksa sessizce atlanir.
# ============================================================

def fetch_jsearch():
    jobs, errors = [], []
    key = os.environ.get("JSEARCH_KEY", "").strip()
    if not key:
        return jobs, ["jsearch: JSEARCH_KEY tanimli degil, atlandi (kurulum: KURULUM.md)"]
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    for lane, cfg in CONFIG["lanes"].items():
        q = cfg["queries"][0]  # kota dostu: lane basina 1 sorgu
        url = ("https://jsearch.p.rapidapi.com/search?query="
               + urllib.parse.quote(q + " in Istanbul, Turkey")
               + "&date_posted=week&num_pages=1&country=tr")
        try:
            data = http_json(url, headers=headers)
            raw = data.get("data", [])
            if not raw:
                errors.append(f"jsearch [{q}]: API calisti, 0 ham sonuc. status={data.get('status')}")
            for r in raw:
                loc = ", ".join(filter(None, [r.get("job_city"), r.get("job_country")]))
                j = make_job(
                    r.get("job_title", ""), r.get("employer_name", ""),
                    loc or "Turkey",
                    r.get("job_apply_link", ""),
                    r.get("job_posted_at_datetime_utc", ""),
                    "google-jobs",
                    clean(r.get("job_description", ""))[:2000],
                )
                if j:
                    jobs.append(j)
        except Exception as e:
            errors.append(f"jsearch [{q}]: {e}")
    return jobs, errors


# ============================================================
# Kaynak: Remotive (resmi public API, remote roller)
# ============================================================

def fetch_remotive():
    jobs, errors = [], []
    if not CONFIG["include_remote_sources"]:
        return jobs, []
    ok_loc = ("europe", "worldwide", "anywhere", "turkey", "emea")
    for lane, cfg in CONFIG["lanes"].items():
        q = cfg["queries"][0]
        url = "https://remotive.com/api/remote-jobs?search=" + urllib.parse.quote(q) + "&limit=20"
        try:
            data = http_json(url)
            for r in data.get("jobs", []):
                loc = (r.get("candidate_required_location") or "").lower()
                if not any(x in loc for x in ok_loc):
                    continue
                j = make_job(
                    r.get("title", ""), r.get("company_name", ""),
                    "Remote (" + (r.get("candidate_required_location") or "?") + ")",
                    r.get("url", ""), r.get("publication_date", ""),
                    "remotive", clean(r.get("description", ""))[:2000],
                )
                if j:
                    jobs.append(j)
        except Exception as e:
            errors.append(f"remotive [{q}]: {e}")
    return jobs, errors


# ============================================================
# Kaynak: Arbeitnow (resmi public API, Avrupa + remote)
# ============================================================

def fetch_arbeitnow():
    jobs, errors = [], []
    if not CONFIG["include_remote_sources"]:
        return jobs, []
    try:
        data = http_json("https://www.arbeitnow.com/api/job-board-api")
        for r in data.get("data", []):
            if not r.get("remote"):
                continue
            created = r.get("created_at")
            posted = ""
            if created:
                posted = datetime.fromtimestamp(created, timezone.utc).isoformat()
            j = make_job(
                r.get("title", ""), r.get("company_name", ""),
                "Remote (" + (r.get("location") or "Europe") + ")",
                r.get("url", ""), posted, "arbeitnow",
                clean(r.get("description", ""))[:2000],
            )
            if j:
                jobs.append(j)
    except Exception as e:
        errors.append(f"arbeitnow: {e}")
    return jobs, errors


# ============================================================
# Kaynak 1: hiring.cafe (resmi olmayan ic API — kirilgan)
# ============================================================

def fetch_hiring_cafe():
    jobs, errors = [], []
    # NOT (2026-07): hiring.cafe eski /api/search-jobs uc noktasini resmen kapatti
    # (HTTP 405). Bagimsiz kaynaklar da bunu dogruluyor. Yeniden aktif etmek icin
    # onceden guncel bir SSR payload / resmi API bulunmasi gerekir.
    if not CONFIG.get("hiring_cafe_enabled", True):
        return jobs, ["hiring.cafe: devre disi (eski API 2026-05'te kirildi, HTTP 405)"]
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
            errors.append(f"greenhouse [{board}]: {e}")
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
            errors.append(f"lever [{co}]: {e}")
    return jobs, errors


# ============================================================
# Ana akis
# ============================================================

def main():
    all_jobs, all_errors, sources = [], [], {}
    for fn in (fetch_jsearch, fetch_themuse, fetch_hiring_cafe, fetch_remotive,
               fetch_arbeitnow, fetch_greenhouse, fetch_lever):
        jobs, errors = fn()
        name = fn.__name__.replace("fetch_", "")
        sources[name] = len(jobs)
        all_jobs.extend(jobs)
        all_errors.extend(errors)

    # Tekillestir (id bazli), fit'e gore sirala
    seen, unique = set(), []
    for j in sorted(all_jobs, key=lambda x: (-x.get("prio",0), -x["fit"])):
        if j["id"] not in seen:
            seen.add(j["id"])
            unique.append(j)

    out = {
        "version": "1.4",
        "fetched_at": NOW.isoformat(),
        "job_count": len(unique),
        "errors": all_errors,
        "sources": sources,
        "jobs": unique,
    }
    with open("data/jobs.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"OK: {len(unique)} ilan yazildi. Hata: {len(all_errors)}")
    for e in all_errors:
        print("  !", e)


if __name__ == "__main__":
    main()
