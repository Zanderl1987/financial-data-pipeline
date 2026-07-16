#!/usr/bin/env python3
"""
BLS Occupational Wages & Employer Costs Pipeline -- OEWS, QCEW, ECEC, CPS demographics.

Extends the BLS pipeline with four new tables from the Bureau of Labor Statistics:

  Table 1: bls_oes (Occupational Employment & Wage Statistics)
    - 800+ occupations: employment, wage percentiles (10th-90th), median/mean wages
    - National level, semi-annual

  Table 2: bls_qcew (Quarterly Census of Employment & Wages)
    - County/MSA employment, wages, establishments (95%+ of US jobs)
    - National, state, MSA, county level

  Table 3: bls_ecec (Employer Costs for Employee Compensation)
    - Employer cost per hour: wages, benefits, insurance, retirement
    - Quarterly, by industry/occupation

  Table 4: bls_cps_demographics (CPS expanded demographics)
    - U-3 and U-6 unemployment rates
    - Median weekly earnings by demographics
    - Labor force participation rate

Uses API v2 if BLS_API_KEY is in .env (higher limits), else v1.
Register free at https://data.bls.gov/registrationEngine/ to get a v2 key.

CLI:
  python bls_oes_qcew_pipeline.py             # incremental (last 2 years)
  python bls_oes_qcew_pipeline.py --backfill  # full history

Outputs:
  storage/raw/bls/oes/bls_oes_{mode}_{YYYYMMDD}.parquet
  storage/raw/bls/qcew/bls_qcew_{mode}_{YYYYMMDD}.parquet
  storage/raw/bls/ecec/bls_ecec_{mode}_{YYYYMMDD}.parquet
  storage/raw/bls/cps_demographics/bls_cps_demographics_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from storage_utils import write_partitioned

load_dotenv()

BLS_API_KEY = os.environ.get("BLS_API_KEY", "")
BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_URL = BLS_V2 if BLS_API_KEY else BLS_V1

BASE_DIR = os.path.join("storage", "raw", "bls")
REQUEST_INTERVAL = 1.5
MAX_RETRIES = 3
BATCH_SIZE = 50 if BLS_API_KEY else 25


# ---------------------------------------------------------------------------
# Table 1: Occupational Employment & Wage Statistics (OEWS)
# National estimates for major occupational groups
# ---------------------------------------------------------------------------

# OEWS series ID format: OEUN0000000000000000{occupation_code}{data_type}
# Data type: 01=Employment, 04=Hourly mean wage, 07=Annual mean wage, 11=Annual median wage

OES_OCCUPATIONS: dict[str, str] = {
    "00000000": "All Occupations",
    "11000000": "Management Occupations",
    "11101100": "Chief Executives",
    "11102100": "General & Operations Managers",
    "11201100": "Advertising & Promotions Managers",
    "11202100": "Marketing Managers",
    "11203100": "Sales Managers",
    "11301100": "Administrative Services Managers",
    "11303100": "Computer & Information Systems Managers",
    "11305100": "Industrial Production Managers",
    "11306100": "Purchasing Managers",
    "11311100": "Property, Real Estate, & Community Association Managers",
    "11312100": "Social & Community Service Managers",
    "11901300": "Farmers, Ranchers, & Other Agricultural Managers",
    "11902100": "Natural Sciences Managers",
    "11903100": "Education Administrators, Postsecondary",
    "11904100": "Education Administrators, Kindergarten through Secondary",
    "11905100": "Education Administrators, All Other",
    "11911100": "Medical & Health Services Managers",
    "11914100": "Postmasters & Mail Superintendents",
    "11915100": "Funeral Service Managers",
    "11916100": "Gaming Managers",
    "11917100": "Lodging Managers",
    "11919100": "Managers, All Other",
    "13000000": "Business & Financial Operations Occupations",
    "13101100": "Agents & Business Managers of Artists, Performers, & Athletes",
    "13102100": "Wholesale & Retail Buyers, Except Farm Products",
    "13102200": "Purchasing Agents, Except Wholesale, Retail, & Farm Products",
    "13103100": "Claims Examiners, Property & Casualty Insurance",
    "13103200": "Insurance Adjusters, Examiners, & Investigators",
    "13104100": "Compliance Officers",
    "13105100": "Cost Estimators",
    "13107100": "Human Resources Specialists",
    "13107200": "Labor Relations Specialists",
    "13107300": "Training & Development Specialists",
    "13107400": "Occupational Analysts",
    "13107500": "Employment Interviewers, Private or Public Employment Service",
    "13107600": "Market Research Analysts & Marketing Specialists",
    "13107700": "Business Operations Specialists, All Other",
    "13108100": "Logisticians",
    "13108200": "Project Management Specialists",
    "13111100": "Management Analysts",
    "13112100": "Meeting, Convention, & Event Planners",
    "13113100": "Fundraisers",
    "13114100": "Compensation, Benefits, & Job Analysis Specialists",
    "13115100": "Training & Development Managers",
    "13116100": "Market Research Analysts & Marketing Specialists",
    "13119900": "Business Operations Specialists, All Other",
    "13201100": "Accountants & Auditors",
    "13202100": "Appraisers & Assessors of Real Estate",
    "13203100": "Budget Analysts",
    "13204100": "Credit Analysts",
    "13205100": "Financial Analysts",
    "13205200": "Personal Financial Advisors",
    "13205300": "Insurance Underwriters",
    "13206100": "Financial Examiners",
    "13207100": "Loan Officers",
    "13207200": "Loan Interviewers & Clerks",
    "13208100": "Tax Examiners, Collectors, & Revenue Agents",
    "13208200": "Tax Preparers",
    "13209900": "Financial Specialists, All Other",
    "15000000": "Computer & Mathematical Occupations",
    "15121100": "Computer Systems Analysts",
    "15121200": "Information Security Analysts",
    "15122100": "Computer & Information Research Scientists",
    "15123100": "Computer Network Support Specialists",
    "15124100": "Computer Occupations, All Other",
    "15124200": "Database Administrators",
    "15124300": "Database Architects",
    "15124400": "Network & Computer Systems Administrators",
    "15125100": "Computer Hardware Engineers",
    "15125200": "Software Quality Assurance Analysts & Testers",
    "15125300": "Computer Programmers",
    "15125400": "Software Developers",
    "15125500": "Software Developers, Applications",
    "15125600": "Software Developers, Systems Software",
    "15125700": "Web Developers",
    "15129900": "Computer Occupations, All Other",
    "15201100": "Actuaries",
    "15202100": "Mathematicians",
    "15203100": "Operations Research Analysts",
    "15204100": "Statisticians",
    "15205100": "Mathematical Science Occupations, All Other",
    "17000000": "Architecture & Engineering Occupations",
    "17101100": "Architects, Except Naval",
    "17101200": "Landscape Architects",
    "17102100": "Cartographers & Photogrammetrists",
    "17102200": "Surveyors",
    "17201100": "Aerospace Engineers",
    "17202100": "Agricultural Engineers",
    "17203100": "Bioengineers & Biomedical Engineers",
    "17204100": "Civil Engineers",
    "17205100": "Computer Hardware Engineers",
    "17206100": "Electrical Engineers",
    "17206200": "Electronics Engineers, Except Computer",
    "17207100": "Environmental Engineers",
    "17207200": "Health & Safety Engineers, Except Mining Safety Engineers & Inspectors",
    "17208100": "Industrial Engineers, Including Health & Safety",
    "17211100": "Marine Engineers & Naval Architects",
    "17212100": "Materials Engineers",
    "17213100": "Mechanical Engineers",
    "17214100": "Mining & Geological Engineers, Including Mining Safety Engineers",
    "17215100": "Nuclear Engineers",
    "17216100": "Petroleum Engineers",
    "17219900": "Engineers, All Other",
    "17221100": "Architectural & Civil Drafters",
    "17222100": "Electrical & Electronics Drafters",
    "17223100": "Mechanical Drafters",
    "17229900": "Drafters, All Other",
    "17231100": "Aerospace Engineering & Operations Technologists & Technicians",
    "17234100": "Civil Engineering Technologists & Technicians",
    "17234200": "Electrical & Electronic Engineering Technologists & Technicians",
    "17234300": "Electromechanical & Mechatronics Technologists & Technicians",
    "17234400": "Environmental Engineering Technologists & Technicians",
    "17234500": "Industrial Engineering Technologists & Technicians",
    "17234600": "Mechanical Engineering Technologists & Technicians",
    "17234700": "Surveying & Mapping Technicians",
    "17239900": "Engineering Technologists & Technicians, Except Drafters, All Other",
    "17301100": "Architectural & Building Designers",
    "17301200": "Cartographers & Photogrammetrists",
    "17301300": "Surveyors",
    "17302100": "Drafters, Except Architectural & Civil",
    "19000000": "Life, Physical, & Social Science Occupations",
    "19101100": "Animal Scientists",
    "19101200": "Food Scientists & Technologists",
    "19101300": "Soil & Plant Scientists",
    "19102100": "Foresters",
    "19102200": "Conservation Scientists",
    "19102300": "Environmental Scientists & Specialists, Including Health",
    "19103100": "Economists",
    "19104100": "Environmental Scientists & Specialists, Including Health",
    "19109900": "Life, Physical, & Social Science Technicians, All Other",
    "19201100": "Astronomers & Physicists",
    "19202100": "Atmospheric & Space Scientists",
    "19203100": "Chemists",
    "19203200": "Materials Scientists",
    "19204100": "Environmental Scientists & Specialists, Including Health",
    "19209900": "Physical Scientists, All Other",
    "19301100": "Economists",
    "19302100": "Market Research Analysts & Marketing Specialists",
    "19305100": "Urban & Regional Planners",
    "19401100": "Agricultural & Food Science Technicians",
    "19402100": "Biological Technicians",
    "19403100": "Chemical Technicians",
    "19405100": "Environmental Science & Protection Technicians, Including Health",
    "19407100": "Forensic Science Technicians",
    "19409900": "Life, Physical, & Social Science Technicians, All Other",
    "25000000": "Education, Training, & Library Occupations",
    "25101100": "Business Teachers, Postsecondary",
    "25102100": "Computer Science Teachers, Postsecondary",
    "25102200": "Mathematical Science Teachers, Postsecondary",
    "25103200": "Engineering Teachers, Postsecondary",
    "25104100": "Architecture Teachers, Postsecondary",
    "25105100": "Agricultural Sciences Teachers, Postsecondary",
    "25105200": "Atmospheric, Earth, Marine, & Space Sciences Teachers, Postsecondary",
    "25105300": "Biological Science Teachers, Postsecondary",
    "25105400": "Chemistry Teachers, Postsecondary",
    "25106100": "Physics Teachers, Postsecondary",
    "25106200": "Environmental Science Teachers, Postsecondary",
    "25106300": "Anthropology & Archeology Teachers, Postsecondary",
    "25106400": "Sociology Teachers, Postsecondary",
    "25106500": "Social Sciences Teachers, Postsecondary, All Other",
    "25106600": "Political Science Teachers, Postsecondary",
    "25106700": "Psychology Teachers, Postsecondary",
    "25106800": "Economics Teachers, Postsecondary",
    "25106900": "Geography Teachers, Postsecondary",
    "25107100": "Health Specialties Teachers, Postsecondary",
    "25107200": "Nursing Instructors & Teachers, Postsecondary",
    "25108100": "Education Teachers, Postsecondary",
    "25108200": "Library Science Teachers, Postsecondary",
    "25108300": "Recreation & Fitness Studies Teachers, Postsecondary",
    "25108400": "Communications Teachers, Postsecondary",
    "25108500": "English Language & Literature Teachers, Postsecondary",
    "25108600": "Foreign Language & Literature Teachers, Postsecondary",
    "25108700": "History Teachers, Postsecondary",
    "25108800": "Philosophy & Religion Teachers, Postsecondary",
    "25108900": "Family & Consumer Sciences Teachers, Postsecondary",
    "25111100": "Law Teachers, Postsecondary",
    "25111200": "Criminal Justice & Law Enforcement Teachers, Postsecondary",
    "25111300": "Social Work Teachers, Postsecondary",
    "25112100": "Art, Drama, & Music Teachers, Postsecondary",
    "25112200": "Communications Teachers, Postsecondary",
    "25112300": "Dance Teachers, Postsecondary",
    "25112400": "Design & Applied Arts Teachers, Postsecondary",
    "25112500": "Film, Video, & Multimedia Arts Teachers, Postsecondary",
    "25112600": "Photography Teachers, Postsecondary",
    "25119900": "Postsecondary Teachers, All Other",
    "25201100": "Preschool Teachers, Except Special Education",
    "25202100": "Kindergarten Teachers, Except Special Education",
    "25202200": "Elementary School Teachers, Except Special Education",
    "25202300": "Middle School Teachers, Except Special & Career/Technical Education",
    "25203100": "Secondary School Teachers, Except Special & Career/Technical Education",
    "25203200": "Career/Technical Education Teachers, Secondary School",
    "25203300": "Special Education Teachers, Preschool, Kindergarten, & Elementary School",
    "25203400": "Special Education Teachers, Middle School",
    "25203500": "Special Education Teachers, Secondary School",
    "25203600": "Special Education Teachers, All Other",
    "25204100": "Self-Enrichment Education Teachers",
    "25205100": "Adult Literacy, Remedial Education, & GED Teachers & Instructors",
    "25301100": "Distance Learning Coordinators",
    "25401100": "Archivists",
    "25401200": "Curators",
    "25401300": "Museum Technicians & Conservators",
    "25402100": "Librarians",
    "25402200": "Library Technicians",
    "25403100": "Library Assistants, Except Clerical",
    "25901100": "Instructional Coordinators",
    "25902100": "Teacher Assistants, Except Special Education",
    "25903100": "Teaching Assistants, Special Education",
    "25904100": "Adult Basic Education, Adult Secondary Education, & Literacy Teachers & Instructors",
    "25909900": "Education, Training, & Library Workers, All Other",
    "29000000": "Healthcare Practitioners & Technical Occupations",
    "29101100": "Chiropractors",
    "29102100": "Dentists, General",
    "29103100": "Dietitians & Nutritionists",
    "29104100": "Optometrists",
    "29105100": "Pharmacists",
    "29106100": "Anesthesiologists",
    "29106200": "Family Medicine Physicians",
    "29106300": "Internists, General",
    "29106400": "Pediatrists, General",
    "29106500": "Psychiatrists",
    "29106600": "Radiologists",
    "29106700": "Surgeons",
    "29106900": "Physicians, All Other",
    "29107100": "Physician Assistants",
    "29108100": "Podiatrists",
    "29111100": "Registered Nurses",
    "29112100": "Nurse Anesthetists",
    "29112200": "Nurse Midwives",
    "29112300": "Nurse Practitioners",
    "29114100": "Occupational Therapists",
    "29115100": "Pharmacists",
    "29117100": "Physical Therapists",
    "29118100": "Speech-Language Pathologists",
    "29121100": "Cardiovascular Technologists & Technicians",
    "29121200": "Diagnostic Medical Sonographers",
    "29121300": "Nuclear Medicine Technologists",
    "29121400": "Radiation Therapists",
    "29121500": "Radiologic Technologists & Technicians",
    "29122100": "Respiratory Therapists",
    "29122200": "Surgical Technologists",
    "29122300": "Veterinary Technologists & Technicians",
    "29124100": "Cardiographic Technologists & Technicians",
    "29124200": "Electroencephalographic Technologists & Technicians",
    "29124300": "Electromyographic Technologists & Technicians",
    "29124400": "Ophthalmic Medical Technologists & Technicians",
    "29124900": "Health Technologists & Technicians, All Other",
    "29125100": "Athletic Trainers",
    "29129200": "Dental Hygienists",
    "29129900": "Healthcare Practitioners & Technical Workers, All Other",
}

OES_DATA_TYPES: dict[str, str] = {
    "01": "Employment",
    "04": "Hourly Mean Wage",
    "07": "Annual Mean Wage",
    "11": "Annual Median Wage",
}


# ---------------------------------------------------------------------------
# Table 2: Quarterly Census of Employment & Wages (QCEW)
# National total private employment
# ---------------------------------------------------------------------------

QCEW_SERIES: dict[str, str] = {
    "ENUUS00010010": "US Total Employment",
    "ENUUS00020010": "US Average Weekly Wage",
    "ENUUS00030010": "US Total Quarterly Wages",
    "ENUUS00040010": "US Average Monthly Employment",
    "ENUUS00050010": "US Number of Establishments",
}


# ---------------------------------------------------------------------------
# Table 3: Employer Costs for Employee Compensation (ECEC)
# Quarterly, by industry
# ---------------------------------------------------------------------------

ECEC_SERIES: dict[str, str] = {
    # Civilian workers
    "CMU01200000000001D": "Civilian Workers - Total Compensation",
    "CMU01200000000002D": "Civilian Workers - Wages & Salaries",
    "CMU01200000000003D": "Civilian Workers - Benefits",
    # Private industry
    "CMU02200000000001D": "Private Industry - Total Compensation",
    "CMU02200000000002D": "Private Industry - Wages & Salaries",
    "CMU02200000000003D": "Private Industry - Benefits",
    # Manufacturing
    "CMU05200000000001D": "Manufacturing - Total Compensation",
    "CMU05200000000002D": "Manufacturing - Wages & Salaries",
    "CMU05200000000003D": "Manufacturing - Benefits",
    # Nonmanufacturing
    "CMU06200000000001D": "Nonmanufacturing - Total Compensation",
    "CMU06200000000002D": "Nonmanufacturing - Wages & Salaries",
    "CMU06200000000003D": "Nonmanufacturing - Benefits",
    # Services
    "CMU04200000000001D": "Services - Total Compensation",
    "CMU04200000000002D": "Services - Wages & Salaries",
    "CMU04200000000003D": "Services - Benefits",
    # State & local government
    "CMU03200000000001D": "State & Local Government - Total Compensation",
    "CMU03200000000002D": "State & Local Government - Wages & Salaries",
    "CMU03200000000003D": "State & Local Government - Benefits",
}


# ---------------------------------------------------------------------------
# Table 4: CPS Expanded Demographics
# Unemployment rates and median weekly earnings
# ---------------------------------------------------------------------------

CPS_SERIES: dict[str, str] = {
    # Unemployment rates
    "LNS14000000":  "Unemployment Rate (U-3, All)",
    "LNS13327709":  "Unemployment Rate (U-6, Broad Measure)",
    "LNS11300000":  "Labor Force Participation Rate",
    "LNS12000000":  "Employment Level",
    # Median weekly earnings by sex
    "LNU04047060":  "Median Weekly Earnings - Men",
    "LNU04047061":  "Median Weekly Earnings - Women",
    # Unemployment by education
    "LNS14027659":  "Unemployment Rate - Less Than High School Diploma",
    "LNS14027660":  "Unemployment Rate - High School Diploma, No College",
    "LNS14027661":  "Unemployment Rate - Some College, No Degree",
    "LNS14027662":  "Unemployment Rate - Associate's Degree",
    "LNS14027663":  "Unemployment Rate - Bachelor's Degree",
    "LNS14027664":  "Unemployment Rate - Advanced Degree",
    # Unemployment by race/ethnicity
    "LNS14000003":  "Unemployment Rate - White",
    "LNS14000006":  "Unemployment Rate - Black or African American",
    "LNS14000009":  "Unemployment Rate - Hispanic or Latino",
    "LNS14000012":  "Unemployment Rate - Asian",
    # Discouraged workers
    "LNU05026645":  "Discouraged Workers",
    # Part time for economic reasons
    "LNS12032194":  "Employed Part Time for Economic Reasons",
}


# ---------------------------------------------------------------------------
# Table configs
# ---------------------------------------------------------------------------

# Build OES series catalog dynamically (must be fully populated before TABLE_CONFIGS
# is constructed below, since TABLE_CONFIGS captures a reference to this dict object)
OES_SERIES: dict[str, tuple[str, str]] = {}
for occ_code, occ_name in OES_OCCUPATIONS.items():
    for dt_code, dt_name in OES_DATA_TYPES.items():
        sid = f"OEUN0000000000000000{occ_code}{dt_code}"
        OES_SERIES[sid] = (f"{occ_name} - {dt_name}", "Varies")

TABLE_CONFIGS = {
    "bls_oes":              (OES_SERIES,  os.path.join(BASE_DIR, "oes")),
    "bls_qcew":             (QCEW_SERIES, os.path.join(BASE_DIR, "qcew")),
    "bls_ecec":             (ECEC_SERIES, os.path.join(BASE_DIR, "ecec")),
    "bls_cps_demographics": (CPS_SERIES,  os.path.join(BASE_DIR, "cps_demographics")),
}


# ---------------------------------------------------------------------------
# HTTP + BLS helpers (identical to bls_expansion_pipeline.py)
# ---------------------------------------------------------------------------

def fetch_batch(series_ids, start_year, end_year):
    payload = {
        "seriesid": series_ids,
        "startyear": str(start_year),
        "endyear": str(end_year),
    }
    if BLS_API_KEY:
        payload["registrationkey"] = BLS_API_KEY
        payload["annualaverage"] = "false"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(BLS_URL, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") != "REQUEST_SUCCEEDED":
                    msgs = data.get("message", [])
                    print(f"  BLS API non-success: {msgs}")
                    return []
                return data.get("Results", {}).get("series", [])
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  429 rate limit. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
                return []
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(30 * attempt)
    return []


def parse_series(raw_series, catalog):
    rows = []
    for s in raw_series:
        sid = s.get("seriesID", "")
        meta = catalog.get(sid)
        if not meta:
            continue
        name, unit = meta
        for obs in s.get("data", []):
            period = obs.get("period", "")
            year_str = obs.get("year", "")
            value_str = obs.get("value", "")
            try:
                value = float(value_str)
                year = int(year_str)
            except (ValueError, TypeError):
                continue
            if period.startswith("M"):
                month = int(period[1:])
                if month > 12:
                    continue
                date_str = f"{year}-{month:02d}-01"
            elif period.startswith("Q"):
                quarter = int(period[1:])
                if quarter > 4:
                    continue  # Q05 = annual average — skip
                month = (quarter - 1) * 3 + 1
                date_str = f"{year}-{month:02d}-01"
            elif period == "A01":
                date_str = f"{year}-01-01"
            else:
                continue
            rows.append({
                "series_id": sid,
                "name":      name,
                "unit":      unit,
                "date":      date_str,
                "period":    period,
                "value":     value,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="BLS OES, QCEW, ECEC, CPS Demographics Pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full history back to 1990")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    current_year = now.year
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"
    start_year = 1990 if args.backfill else current_year - 2

    print(f"BLS OES/QCEW/ECEC/CPS Pipeline  mode={mode}  start={start_year}")
    print(f"API: {'v2 (key present)' if BLS_API_KEY else 'v1 (no key)'}\n")

    year_chunks = []
    SPAN = 20 if BLS_API_KEY else 10
    y = start_year
    while y <= current_year:
        year_chunks.append((y, min(y + SPAN - 1, current_year)))
        y += SPAN

    for table_name, (catalog, output_dir) in TABLE_CONFIGS.items():
        if not catalog:
            print(f"[{table_name}]  No series defined, skipping.\n")
            continue
        os.makedirs(output_dir, exist_ok=True)
        series_list = list(catalog.keys())
        print(f"[{table_name}]  {len(series_list)} series, {len(year_chunks)} year chunk(s)...")

        all_frames = []
        for y_start, y_end in year_chunks:
            for batch_start in range(0, len(series_list), BATCH_SIZE):
                batch = series_list[batch_start:batch_start + BATCH_SIZE]
                raw = fetch_batch(batch, y_start, y_end)
                if raw:
                    df = parse_series(raw, catalog)
                    if not df.empty:
                        all_frames.append(df)
                time.sleep(REQUEST_INTERVAL)

        if not all_frames:
            print(f"  No data returned.\n")
            continue

        combined = (
            pd.concat(all_frames, ignore_index=True)
            .drop_duplicates(subset=["series_id", "date"])
            .sort_values(["series_id", "date"])
        )
        combined["fetched_at"] = now.isoformat()

        path = write_partitioned(
            combined, output_dir,
            f"{table_name}_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(combined):,} rows, {combined['series_id'].nunique()} series)\n")

    print("--- BLS OES/QCEW/ECEC/CPS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
