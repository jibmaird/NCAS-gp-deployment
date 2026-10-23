#!/usr/bin/env python3
"""Map the free-text indications in the NCAS DSI Patron database to the GP NAPS indications list.

    RFP  Medications.RX_HIST  medication reason (column found automatically)  -> Indication_RFP
    AR   Medications.RX_HIST  Authority_Reason                                -> Indication_AR
    RFV  Encounter.ENC_RSN    Source_Visit_Reason_Description                 -> Indication_RFV

A text is matched against the gp_stream manual mapping (gp_naps_manual_mapping.csv) in three steps, each
only for texts the previous one did not match (KEY_STEPS): equal ignoring case, spaces and punctuation; then
also ignoring digits and counts (x2), laterality and uncertainty words and plural -s; then also ignoring
word order. Failing
those, it is matched against the names on the GP NAPS indications list (gp_naps_lists.csv). It then gets that entry's indication(s), plus any qualifiers,
pathogens and signs/symptoms/mechanisms (SSM) annotated with it, as list names and SNOMED codes.
PBS "criteria ... must ...; OR ..." boilerplate is never matched: it lists alternative conditions, so
the text alone can't say which one applied.

Writes to --out-dir:
    rx_hist_indications.csv   one row per prescription: key columns + RFP and AR columns
    enc_rsn_indications.csv   one row per visit reason: key columns + RFV columns
    unmatched_terms.csv       review sheet for the domain expert: one row per distinct unmatched text (after
                              normalisation), alphabetical. As a suggestion it shows the closest manual-mapping
                              text and its annotation, and the closest indications-list name, all as names (no
                              SNOMED codes), with the number of characters different (edit distance on the
                              normalised forms). Blank columns take the expert's Y/N for each suggestion and,
                              when neither is correct, their own annotation.
    summary.txt               counts only, safe to export
With --terms-only the two record-level files are skipped and only distinct texts are read (much faster).

Usage:  python map_indications.py --server HOST [--terms-only] [--top N] [--fields RFP AR RFV]
                                   [--rfv-scope linked|window]
Needs:  pyodbc; rapidfuzz is used if installed (much faster edit distances), otherwise the edit distances
        run on all CPU cores but one (--workers N to change, --workers 1 if run from an IDE or notebook)
"""
import argparse, csv, datetime, functools, hashlib, multiprocessing, os, re, sys, time, unicodedata
from collections import Counter, defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RX_HIST = '[Medications].[RX_HIST]'
ENC_RSN = '[Encounter].[ENC_RSN]'
RFP_COLUMNS = ['Medication_Reason_Description', 'Source_Medication_Reason_Description']  # Sheet1 vs Sheet3 spelling
RX_KEYS = ['Site_PPN', 'Patient_PPN', 'Visit_ID', 'Script_ID', 'Script_Date', 'Source_System']
ENC_KEYS = ['Site_PPN', 'Patient_PPN', 'Visit_ID', 'Visit_Reason_Date', 'Source_System']
FIELD_TABLE = {'RFP': 'rx', 'AR': 'rx', 'RFV': 'enc'}
FIELD_LABELS = {'RFP': 'reason for prescription', 'AR': 'authority reason', 'RFV': 'reason for visit'}
# Expert's annotation when neither suggestion is correct, named as in the workbook's 'Mapping (combined)' sheet.
EXPERT_COLUMNS = ['Annotated indications', 'Annotated Signs, symp, mech', 'Annotated qualifiers', 'Annotated Pathogen',
                  'Comment']
GROUPS = ['Indication', 'Qualifier', 'Pathogen', 'SSM']
VALUE_COLUMNS = [g + s for g in GROUPS for s in ('', '_SNOMED')]
EMPTY = dict.fromkeys(VALUE_COLUMNS, '')
NON_RELEVANT = 'Non-relevant data'
STATUSES = ['manual_mapping', 'indication_list', 'unmatched', 'criteria_template', 'blank']
MATCHED = ['manual_mapping', 'indication_list']
DISTANCE_BINS = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 10), (11, 20), (21, None)]  # characters different

# Same pattern as gp_stream/Metamap/string_match_fallback.py, plus DOTALL for multi-line database text.
TEMPLATE_RE = re.compile(r'\bcriteria\b.*\bmust\b.*;\s*or\b', re.IGNORECASE | re.DOTALL)
_WORD_RE = re.compile(r'[a-z]+')
# Counts such as "x2", "X 3" or "4x" (times n): dropped together with their number, like other digits.
_COUNT_RE = re.compile(r'(?<![a-z])(?:x\s*\d+|\d+\s*x)(?![a-z])')
# Words that never change the indication, so matching ignores them. Laterality: the word list of gp_stream's
# laterality review tier, ruled on by the domain expert. Uncertainty: protocol v1.1 ignores diagnostic uncertainty
# ('?', 'query', 'vs'). Checked against the reference standard: no texts that differ only in these words, in
# digits, in a plural -s or in word order carry different indications (word order since the expert's review of
# 2026-10-23, applied through reference_corrections.csv).
LATERALITY_WORDS = {'left', 'right', 'bilateral', 'unilateral'}
UNCERTAINTY_WORDS = {'query', 'vs', 'versus', 'possible', 'possibly', 'probable', 'probably', 'suspected', 'suspect',
                     'likely', 'presumed'}
IGNORED_WORDS = LATERALITY_WORDS | UNCERTAINTY_WORDS


def _singular(word):
    return word[:-1] if len(word) > 3 and word.endswith('s') and not word.endswith('ss') else word


def _ascii_lower(text):
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii').lower()


_SPELLING_DROP_RE = re.compile(r'[^a-z0-9]')


def spelling_key(text):
    """Step 1, the original rule: lowercase, accents stripped, everything except a-z and 0-9 dropped."""
    return _SPELLING_DROP_RE.sub('', _ascii_lower(text))


def _words(text):
    """Lowercase words (letters only) without counts, laterality or uncertainty words and with any plural -s
    removed. Apostrophes are joined into their word and a lone 's' is dropped, so "Meniere's" = "Menieres" and
    "Corn(s)" = "Corns"."""
    text = _COUNT_RE.sub(' ', _ascii_lower(text).replace("'", ''))
    return [_singular(w) for w in _WORD_RE.findall(text) if w not in IGNORED_WORDS and w != 's']


def normalize(text):
    """Step 2: the words run together, so case, spaces, punctuation, digits and counts, laterality and uncertainty
    words and plural -s are all ignored."""
    return ''.join(_words(text))


def word_order_key(text):
    """Step 3: as normalize, with the words sorted first, so "Ulcer, foot" = "Foot ulcer"."""
    return ''.join(sorted(_words(text)))


# Tried in this order; each step only for texts the previous ones did not match. Word order needs its own step
# because sorting breaks space-insensitivity ("Head ache" vs "Headache"), which the earlier steps keep.
KEY_STEPS = [('spelling', spelling_key), ('normalised', normalize), ('word order', word_order_key)]


def is_criteria_template(text):
    return bool(TEMPLATE_RE.search(text))


def load_reference(folder):
    """-> index {step: {key: output values}} of the manual mapping, suggestions {normalised key: (reference text,
    output values)}, and list_index {step: {key: (name, codes)}} of the indication-list names."""
    def rows(name):
        with open(os.path.join(folder, name), newline='', encoding='utf-8-sig') as f:
            return list(csv.DictReader(f))

    def check_codes(value, where):
        if re.search(r'[^0-9;]', value):
            sys.exit(f'{where} has a malformed SNOMED code {value!r}. The reference files must not be opened '
                     'and re-saved in Excel, which rounds long codes.')
        return value

    list_index = {step: {} for step, _ in KEY_STEPS[1:]}
    for r in rows('gp_naps_lists.csv'):
        if r['list'] == 'indication' and r['name'] != NON_RELEVANT:
            entry = (r['name'], check_codes(r['snomed_codes'], 'gp_naps_lists.csv'))
            for step, key_of in KEY_STEPS[1:]:
                list_index[step][key_of(r['name'])] = entry
    index, suggestions = {step: {} for step, _ in KEY_STEPS}, {}
    for r in rows('gp_naps_manual_mapping.csv'):
        for c in VALUE_COLUMNS:
            if c.endswith('_SNOMED'):
                check_codes(r[c], 'gp_naps_manual_mapping.csv')
        values = {c: r[c] for c in VALUE_COLUMNS}
        for step, key_of in KEY_STEPS:
            if index[step].setdefault(key_of(r['reference_text']), values) != values:
                sys.exit(f'gp_naps_manual_mapping.csv: texts sharing the {step} key of {r["reference_text"]!r} '
                         'carry different annotations, so the file has been edited. Rebuild it with '
                         'build_sre_reference.py.')
        suggestions.setdefault(normalize(r['reference_text']), (r['reference_text'], values))
    return index, suggestions, list_index


class Mapper:
    def __init__(self, index, list_index):
        self.index, self.list_index = index, list_index
        self.classify = functools.lru_cache(maxsize=1 << 19)(self._classify)

    def _classify(self, text):
        """-> (normalised key, status, output values or None)"""
        if text is None or not str(text).strip():
            return '', 'blank', None
        text = str(text)
        key = normalize(text)
        if is_criteria_template(text):
            return key, 'criteria_template', None
        for step, key_of in KEY_STEPS:
            values = self.index[step].get(key_of(text))
            if values is not None:
                return key, 'manual_mapping', values
        for step, key_of in KEY_STEPS[1:]:
            entry = self.list_index[step].get(key_of(text))
            if entry is not None:
                return key, 'indication_list', dict(EMPTY, Indication=entry[0], Indication_SNOMED=entry[1])
        return key, 'unmatched', None


class Tally:
    """Rows and distinct keys per field and status, plus the spellings and per-field counts of unmatched keys."""
    def __init__(self, mapper, fields):
        self.mapper = mapper
        self.rows = {f: Counter() for f in fields}
        self.keys = {f: defaultdict(set) for f in fields}
        self.unmatched = {}  # key -> [Counter(spelling -> rows), Counter(field -> rows), is criteria template]

    def add(self, field, text, n=1):
        key, status, values = self.mapper.classify(text)
        self.rows[field][status] += n
        self.keys[field][status].add(key)
        if status in ('unmatched', 'criteria_template'):
            entry = self.unmatched.setdefault(key, [Counter(), Counter(), False])
            entry[0][text] += n
            entry[1][field] += n
            entry[2] = entry[2] or status == 'criteria_template'
        return status, values


def levenshtein(peq, m, b):
    """Edit distance between a pattern of length m, given as its bit masks peq, and string b (Hyyrö 2003)."""
    if m == 0:
        return len(b)
    mask, last = (1 << m) - 1, 1 << (m - 1)
    pv, mv, score = mask, 0, m
    for ch in b:
        eq = peq.get(ch, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = mv | ~(xh | pv)
        mh = pv & xh
        if ph & last:
            score += 1
        elif mh & last:
            score -= 1
        ph = (ph << 1) | 1
        mh <<= 1
        pv = (mh | ~(xv | ph)) & mask
        mv = ph & xv
    return score


class Nearest:
    """Closest choice by edit distance (ties go to the earlier choice), via rapidfuzz when installed."""
    def __init__(self, choices):
        self.choices = choices
        try:
            from rapidfuzz import process
            from rapidfuzz.distance import Levenshtein
            self.engine, self._rapidfuzz = 'rapidfuzz', (process, Levenshtein.distance)
        except ImportError:
            self.engine, self._rapidfuzz = 'built-in (pip install rapidfuzz for speed)', None
            self.by_length = defaultdict(list)
            for i, c in enumerate(choices):
                self.by_length[len(c)].append(i)

    def find(self, query):
        """-> (index of the closest choice, distance)"""
        if self._rapidfuzz:
            process, distance = self._rapidfuzz
            _, d, i = process.extractOne(query, self.choices, scorer=distance, processor=None)
            return i, int(d)
        m = len(query)
        peq = {}
        for i, ch in enumerate(query):
            peq[ch] = peq.get(ch, 0) | (1 << i)
        best_d, best_i = sys.maxsize, -1
        # The length difference is a lower bound on the distance, so stop once it exceeds the best so far.
        for n in sorted(self.by_length, key=lambda n: abs(n - m)):
            if abs(n - m) > best_d:
                break
            for i in self.by_length[n]:
                d = levenshtein(peq, m, self.choices[i])
                if d < best_d or (d == best_d and i < best_i):
                    best_d, best_i = d, i
        return best_i, best_d


def bracketed(table):
    """'Schema.Table' or '[Schema].[Table]' -> '[Schema].[Table]'."""
    schema, name = table.replace('[', '').replace(']', '').split('.', 1)
    return f'[{schema}].[{name}]'


def table_columns(cur, table):
    schema, name = table.strip('[]').split('].[')
    rows = cur.execute('SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?',
                       schema, name).fetchall()
    if not rows:
        sys.exit(f'Table {table} was not found, or you have no access to it. If it has another name, give it with '
                 '--rx-table or --enc-table (Schema.Table).')
    return {r[0].lower(): r[0] for r in rows}


def file_md5(path):
    with open(path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


def require(cols, name, table):
    if name.lower() not in cols:
        sys.exit(f'{table} has no column {name}. Its columns: {sorted(cols.values())}. If a source column has '
                 'another name, give it with --rfp-column, --ar-column or --rfv-column.')
    return cols[name.lower()]


def rx_filter(alias, rx_cols, args):
    date = require(rx_cols, 'Script_Date', RX_HIST)
    clauses, params = [f'{alias}.[{date}] >= ?', f'{alias}.[{date}] < ?'], [args.date_from, args.date_to_excl]
    if args.source_systems:
        system = require(rx_cols, 'Source_System', RX_HIST)
        clauses.append(f'{alias}.[{system}] IN ({", ".join("?" * len(args.source_systems))})')
        params += args.source_systems
    if args.rx_condition:
        clauses.append(f'({args.rx_condition})')
    return ' AND '.join(clauses), params


def enc_filter(rx_cols, enc_cols, args, say):
    if args.rfv_scope == 'linked':
        where, params = rx_filter('r', rx_cols, args)
        patient, visit = require(enc_cols, 'Patient_PPN', ENC_RSN), require(enc_cols, 'Visit_ID', ENC_RSN)
        return (f'EXISTS (SELECT 1 FROM {RX_HIST} r WHERE r.[{require(rx_cols, "Patient_PPN", RX_HIST)}] = x.[{patient}] '
                f'AND r.[{require(rx_cols, "Visit_ID", RX_HIST)}] = x.[{visit}] AND {where})'), params
    date = require(enc_cols, 'Visit_Reason_Date', ENC_RSN)
    clauses, params = [f'x.[{date}] >= ?', f'x.[{date}] < ?'], [args.date_from, args.date_to_excl]
    if args.source_systems:
        if 'source_system' in enc_cols:
            clauses.append(f'x.[{enc_cols["source_system"]}] IN ({", ".join("?" * len(args.source_systems))})')
            params += args.source_systems
        else:
            say('  note: ENC_RSN has no Source_System column, so visit reasons are filtered by date only')
    return ' AND '.join(clauses), params


def stream(cur, sql, params, label, say, batch=20000):
    start, n = time.time(), 0
    cur.execute(sql, params)
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            break
        for r in rows:
            yield r
        n += len(rows)
        if n % 1000000 < batch:
            print(f'    {label}: {n:,} rows read ({time.time() - start:,.0f}s)')
    say(f'  {label}: {n:,} rows read in {time.time() - start:,.0f}s')


def field_header(field, include_text):
    return ([f'Indication_{field}_match'] + [f'{g}_{field}{s}' for g in GROUPS for s in ('', '_SNOMED')]
            + ([f'text_{field}'] if include_text else []))


def field_values(tally, field, text, include_text):
    status, values = tally.add(field, text)
    values = values or EMPTY
    return [status] + [values[c] for c in VALUE_COLUMNS] + ([text] if include_text else [])


def write_records(path, cur, sql, params, keys, fields, tally, args, say):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(keys + [h for field in fields for h in field_header(field, args.include_text)])
        for r in stream(cur, sql, params, os.path.basename(path), say):
            row = ['' if v is None else v for v in r[:len(keys)]]
            for field, text in zip(fields, r[len(keys):]):
                row += field_values(tally, field, text, args.include_text)
            w.writerow(row)


_nearest = None  # (closest reference text, closest indication name) finders, one pair per process


def _start_finders(ref_keys, ind_keys):
    global _nearest
    _nearest = (Nearest(ref_keys), Nearest(ind_keys))


def _find_both(key):
    """-> (index, distance) of the closest reference text, then of the closest indication name."""
    return _nearest[0].find(key) + _nearest[1].find(key)


def _with_progress(results, total, start):
    for done, result in enumerate(results, 1):
        if done % 2000 == 0:
            print(f'    {done:,}/{total:,} ({(total - done) * (time.time() - start) / done:,.0f}s left)')
        yield result


def write_unmatched(path, tally, suggestions, indications, fields, workers, say):
    """Review sheet for the domain expert: names only, never SNOMED codes, and blank answer columns laid out
    like the 'Mapping (combined)' sheet so the answers can be merged into the manual mapping."""
    ref_keys, ind_keys = sorted(suggestions), sorted(indications)
    _start_finders(ref_keys, ind_keys)
    keys = list(tally.unmatched)
    # The built-in engine is pure Python, so spread it over processes; rapidfuzz is fast enough on one.
    parallel = _nearest[0].engine != 'rapidfuzz' and workers > 1 and len(keys) >= 1000
    say(f'\nFinding the closest reference entries for {len(keys):,} unmatched texts '
        f'(edit distance: {_nearest[0].engine}{f", {workers} processes" if parallel else ""})')
    start, rows, misses = time.time(), [], []
    if parallel:
        with multiprocessing.Pool(workers, _start_finders, (ref_keys, ind_keys)) as pool:
            found = list(_with_progress(pool.imap(_find_both, keys, chunksize=50), len(keys), start))
    else:
        found = list(_with_progress(map(_find_both, keys), len(keys), start))
    for key, (ri, rd, ii, idist) in zip(keys, found):
        spellings, per_field, template = tally.unmatched[key]
        misses.append((template, rd, idist, sum(per_field.values())))
        suggested_text, suggestion = suggestions[ref_keys[ri]]
        term = ' '.join(spellings.most_common(1)[0][0].split())
        rows.append([term, sum(per_field.values())] + [per_field[f] for f in fields] + [
            'yes' if template else '', suggested_text]
            + [suggestion[g].replace(';', ' + ') for g in GROUPS]
            + [rd, '', indications[ind_keys[ii]][0], idist, ''] + [''] * len(EXPERT_COLUMNS))
    # Alphabetical as a reader expects it: case-insensitive, ignoring leading punctuation such as '?'.
    rows.sort(key=lambda r: (re.sub(r'^[^a-z0-9]+', '', r[0].casefold()), r[0]))
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['Free text', 'Records'] + [f'Records ({FIELD_LABELS[f]})' for f in fields] + [
            'PBS criteria text', 'Suggestion 1: closest annotated text', 'Suggestion 1: indication(s)',
            'Suggestion 1: qualifier(s)', 'Suggestion 1: pathogen(s)', 'Suggestion 1: signs/symptoms/mechanisms',
            'Suggestion 1: characters different', 'Suggestion 1 correct? (Y/N)',
            'Suggestion 2: closest indication-list name', 'Suggestion 2: characters different',
            'Suggestion 2 correct? (Y/N)'] + EXPERT_COLUMNS)
        w.writerows(rows)
    say(f'  done in {time.time() - start:,.0f}s')
    return misses


def pct(part, whole):
    return f'{100 * part / whole:.1f}%' if whole else '-'


def status_table(say, title, records, texts):
    """Records and distinct texts per match status, with percentages (texts exclude blanks, which have none)."""
    n_records, n_texts = sum(records.values()), sum(texts[s] for s in STATUSES if s != 'blank')
    say(f'\n  {title}')
    say(f'    {"":<18} {"records":>14} {"%":>7}   {"distinct texts":>14} {"%":>7}')
    for s in STATUSES + ['total']:
        r = n_records if s == 'total' else records[s]
        t = n_texts if s == 'total' else texts[s]
        distinct = '' if s == 'blank' else f'{t:>14,} {pct(t, n_texts):>7}'
        say(f'    {s:<18} {r:>14,} {pct(r, n_records):>7}   {distinct}')
    say(f'    matched: {pct(sum(records[s] for s in MATCHED), n_records - records["blank"])} of non-blank records, '
        f'{pct(sum(texts[s] for s in MATCHED), n_texts)} of distinct texts')


def distance_table(say, title, misses):
    """misses: [(is PBS criteria text, characters different, records)]. Percentages are of all misses."""
    counts = [[0, 0] for _ in DISTANCE_BINS]
    for template, d, n in misses:
        if not template:
            i = next(i for i, (_, hi) in enumerate(DISTANCE_BINS) if hi is None or d <= hi)
            counts[i][0] += 1
            counts[i][1] += n
    n_texts, n_records = len(misses), sum(n for _, _, n in misses)
    say(f'\n  {title}')
    say(f'    {"characters":<14} {"texts":>10} {"%":>7} {"cum. %":>7}   {"records":>14} {"%":>7} {"cum. %":>7}')
    cum_texts = cum_records = 0
    for (lo, hi), (t, r) in zip(DISTANCE_BINS, counts):
        cum_texts, cum_records = cum_texts + t, cum_records + r
        label = str(lo) if lo == hi else f'{lo}+' if hi is None else f'{lo}-{hi}'
        say(f'    {label:<14} {t:>10,} {pct(t, n_texts):>7} {pct(cum_texts, n_texts):>7}   '
            f'{r:>14,} {pct(r, n_records):>7} {pct(cum_records, n_records):>7}')
    templates = [n for template, _, n in misses if template]
    say(f'    {"PBS criteria":<14} {len(templates):>10,} {pct(len(templates), n_texts):>7} {"":>7}   '
        f'{sum(templates):>14,} {pct(sum(templates), n_records):>7}')
    say(f'    {"total":<14} {n_texts:>10,} {pct(n_texts, n_texts):>7} {"":>7}   '
        f'{n_records:>14,} {pct(n_records, n_records):>7}')


def main():
    global RX_HIST, ENC_RSN
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--server', required=True, help='SQL Server host (kept out of the code, which is public)')
    ap.add_argument('--database', default='NCAS_DSI_Patron')
    ap.add_argument('--driver', default='ODBC Driver 17 for SQL Server')
    ap.add_argument('--fields', nargs='+', choices=['RFP', 'AR', 'RFV'], default=['RFP', 'AR', 'RFV'])
    ap.add_argument('--date-from', type=datetime.date.fromisoformat, default=datetime.date(2018, 1, 1))
    ap.add_argument('--date-to', type=datetime.date.fromisoformat, default=datetime.date(2022, 12, 31),
                    help='inclusive')
    ap.add_argument('--source-systems', nargs='*', default=['MD', 'BP'],
                    help='RX_HIST.Source_System values to keep; give the flag with no values to keep all')
    ap.add_argument('--rfv-scope', choices=['linked', 'window'], default='linked',
                    help='linked: visit reasons for visits with a prescription in the filters above (same '
                         'Patient_PPN and Visit_ID); window: all visit reasons dated within the date range')
    ap.add_argument('--rx-condition', help='extra SQL condition on the prescriptions, with unqualified column names, '
                                           'e.g. "Medication_Generic_Name IN (\'amoxicillin\', \'cefalexin\')"')
    ap.add_argument('--no-list-names', action='store_true',
                    help='do not match texts to indication-list names, only to the manual mapping')
    ap.add_argument('--rx-table', default=RX_HIST, help='prescriptions table, as Schema.Table')
    ap.add_argument('--enc-table', default=ENC_RSN, help='visit-reasons table, as Schema.Table')
    ap.add_argument('--rfp-column', help='RX_HIST medication-reason column (default: found automatically)')
    ap.add_argument('--ar-column', default='Authority_Reason', help='RX_HIST authority-reason column')
    ap.add_argument('--rfv-column', default='Source_Visit_Reason_Description', help='ENC_RSN visit-reason column')
    ap.add_argument('--terms-only', action='store_true', help='skip the record-level files; read distinct texts only')
    ap.add_argument('--top', type=int, help='read at most N rows (or N distinct texts) per query, for a trial run')
    ap.add_argument('--include-text', action='store_true', help='also copy the original text into the record files')
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1),
                    help='processes for the edit distances when rapidfuzz is not installed (default: all cores but one)')
    ap.add_argument('--reference-dir', default=SCRIPT_DIR, help='folder with the two gp_naps_*.csv files')
    ap.add_argument('--out-dir', help='default: indication_mapping_<timestamp>')
    args = ap.parse_args()
    if args.rx_condition and re.search(r';|--|/\*', args.rx_condition):
        sys.exit('--rx-condition must be a single condition, without ";" or SQL comments: the script only reads.')
    args.date_to_excl = args.date_to + datetime.timedelta(days=1)
    RX_HIST, ENC_RSN = bracketed(args.rx_table), bracketed(args.enc_table)
    out_dir = args.out_dir or f'indication_mapping_{datetime.datetime.now():%Y%m%d_%H%M%S}'
    os.makedirs(out_dir, exist_ok=True)

    report = []

    def say(line=''):
        print(line)
        report.append(line)

    index, suggestions, list_index = load_reference(args.reference_dir)
    say(f'Run {datetime.datetime.now():%Y-%m-%d %H:%M}: {"terms only" if args.terms_only else "record level"}'
        f'{f", TOP {args.top}" if args.top else ""}; fields {" ".join(args.fields)}')
    say(f'Reference: {len(index["spelling"]):,} manual-mapping texts (distinct spellings), '
        f'{len(list_index["normalised"])} indication names')
    say('Files (MD5): ' + ', '.join(f'{os.path.basename(p)} {file_md5(p)}' for p in [
        os.path.abspath(__file__), os.path.join(args.reference_dir, 'gp_naps_manual_mapping.csv'),
        os.path.join(args.reference_dir, 'gp_naps_lists.csv')]))
    say(f'Prescriptions dated {args.date_from} to {args.date_to}, Source_System in {args.source_systems or "(any)"}'
        f'{f", and {args.rx_condition}" if args.rx_condition else ""}; visit reasons: {args.rfv_scope}; '
        f'indication-list names {"not " if args.no_list_names else ""}matched')

    import pyodbc
    cnx = pyodbc.connect(f'DRIVER={{{args.driver}}};SERVER={args.server};DATABASE={args.database};'
                         'Trusted_Connection=yes;TrustServerCertificate=yes')
    cur = cnx.cursor()
    rx_cols = table_columns(cur, RX_HIST)
    enc_cols = table_columns(cur, ENC_RSN) if 'RFV' in args.fields else {}
    source = {}
    if 'RFP' in args.fields:
        rfp = args.rfp_column or next((c for c in RFP_COLUMNS if c.lower() in rx_cols), None)
        source['RFP'] = require(rx_cols, rfp or RFP_COLUMNS[0], RX_HIST)
    if 'AR' in args.fields:
        source['AR'] = require(rx_cols, args.ar_column, RX_HIST)
    if 'RFV' in args.fields:
        source['RFV'] = require(enc_cols, args.rfv_column, ENC_RSN)
    say(f'Source columns: {source}')

    rx_where, rx_params = rx_filter('x', rx_cols, args)
    enc_where, enc_params = enc_filter(rx_cols, enc_cols, args, say) if 'RFV' in args.fields else ('', [])
    table_sql = {'rx': (RX_HIST, rx_where, rx_params), 'enc': (ENC_RSN, enc_where, enc_params)}
    top = f'TOP ({args.top}) ' if args.top else ''
    tally = Tally(Mapper(index, {step: {} for step in list_index} if args.no_list_names else list_index), args.fields)

    say('\nReading the database')
    if args.terms_only:
        for field in args.fields:
            table, where, params = table_sql[FIELD_TABLE[field]]
            text = f'CAST(x.[{source[field]}] AS nvarchar(max))'
            sql = f'SELECT {top}{text}, COUNT_BIG(*) FROM {table} x WHERE {where} GROUP BY {text}'
            for t, n in stream(cur, sql, params, f'distinct {field} texts', say):
                tally.add(field, t, n)
    else:
        for table_id, key_names, name in [('rx', RX_KEYS, 'rx_hist_indications.csv'),
                                          ('enc', ENC_KEYS, 'enc_rsn_indications.csv')]:
            fields = [f for f in args.fields if FIELD_TABLE[f] == table_id]
            if not fields:
                continue
            table, where, params = table_sql[table_id]
            cols = rx_cols if table_id == 'rx' else enc_cols
            keys = [cols[k.lower()] for k in key_names if k.lower() in cols]
            select = ', '.join(f'x.[{c}]' for c in keys + [source[f] for f in fields])
            write_records(os.path.join(out_dir, name), cur, f'SELECT {top}{select} FROM {table} x WHERE {where}',
                          params, keys, fields, tally, args, say)

    misses = write_unmatched(os.path.join(out_dir, 'unmatched_terms.csv'), tally, suggestions,
                             list_index['normalised'], args.fields, args.workers, say)

    say('\nMatches by field (a distinct text is one normalised form; percentages are of the total)')
    for field in args.fields:
        status_table(say, f'{field} ({source[field]})', tally.rows[field],
                     {s: len(tally.keys[field][s]) for s in STATUSES})
    if len(args.fields) > 1:
        status_table(say, 'All fields', sum(tally.rows.values(), Counter()),
                     {s: len(set().union(*(tally.keys[f][s] for f in args.fields))) for s in STATUSES})

    say('\nMisses by characters different from the closest suggestion (percentages are of all misses)')
    distance_table(say, 'Suggestion 1: closest annotated text', [(t, d, n) for t, d, _, n in misses])
    distance_table(say, 'Suggestion 2: closest indication-list name', [(t, d, n) for t, _, d, n in misses])
    say(f'\nOutputs in {os.path.abspath(out_dir)}')
    with open(os.path.join(out_dir, 'summary.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(report) + '\n')


if __name__ == '__main__':
    main()
