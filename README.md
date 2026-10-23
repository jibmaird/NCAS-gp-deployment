# NCAS GP deployment

Deployment code for the NCAS general-practice indication mapping in the SRE. The research code for the paper is
in [NCAS-GP-indication-classifier](https://github.com/jibmaird/NCAS-GP-indication-classifier).

`map_indications.py` maps the free-text indication fields of general-practice prescriptions in the NCAS DSI Patron
database onto the GP NAPS indications list, for the National Centre for Antimicrobial Stewardship (NCAS). It runs
inside the University of Melbourne Secure Research Environment (SRE).

It reads three free-text fields:
- the reason for prescription and the PBS authority reason, from `Medications.RX_HIST`;
- the reason for visit, from `Encounter.ENC_RSN`.

Each text that matches a reference list of expert-annotated texts gets the matching GP NAPS indication(s) and SNOMED
CT-AU codes. Texts that don't match go to a review sheet for a domain expert.

The two reference files it needs (`gp_naps_manual_mapping.csv`, `gp_naps_lists.csv`) are project data. They are not
in this repository; they are provided separately inside the SRE.

## Installation

Requirements:
- Python 3.8 or later (tested on 3.8, 3.9 and 3.13);
- `pyodbc` with Microsoft ODBC Driver 17 for SQL Server;
- optionally `rapidfuzz` (`pip install rapidfuzz`), which makes the closest-match search about 30 times faster.

Steps:
1. Download version 1.0.0 of the script:
   https://raw.githubusercontent.com/jibmaird/NCAS-gp-deployment/v1.0.0/map_indications.py
2. Save it in the project folder that holds `gp_naps_manual_mapping.csv` and `gp_naps_lists.csv`.
3. Check the download: `certutil -hashfile map_indications.py MD5` should print
   `98ec642e9fc2c629abbf03719890dff0`.

The script needs no other installation steps: nothing is built, and no administrator rights are needed to run it.

## What it accesses

- **Database:** `SELECT` queries only, on the two tables above, using the user's Windows login. It never writes to the
  database.
- **Files:** it reads the two reference CSVs and writes its results as CSV and text files into a new folder under
  the current directory.
- **Network:** the database connection is its only one.

## Running

```
python map_indications.py --server <SQL Server host> --terms-only --top 1000   # quick trial
python map_indications.py --server <SQL Server host> --terms-only              # match rates and review sheet
python map_indications.py --server <SQL Server host>                           # one output row per record
python map_indications.py --help                                               # all options
```

`summary.txt` in the output folder records the run, including the MD5 of the script and of both reference files.
