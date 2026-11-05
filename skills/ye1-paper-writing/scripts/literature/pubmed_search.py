#!/usr/bin/env python3
"""PubMed retrieval with history-backed pagination and an auditable screening queue.

Only the standard library is needed. An agent designs the query and reviews records;
the script never makes clinical inclusion decisions.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
MAX_ES = 10000
MIN_INTERVAL = 0.38
LAST = 0.0


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def text(node):
    return ' '.join(''.join(node.itertext()).split()) if node is not None else ''


def request(endpoint, params):
    global LAST
    params = dict(params, db='pubmed', tool='medical_literature_research_skill')
    email = os.environ.get('NCBI_EMAIL')
    api_key = os.environ.get('NCBI_API_KEY')
    if email:
        params['email'] = email
    if api_key:
        params['api_key'] = api_key
    data = urllib.parse.urlencode(params).encode('utf-8')
    for attempt in range(5):
        elapsed = time.monotonic() - LAST
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        LAST = time.monotonic()
        try:
            req = urllib.request.Request(BASE + endpoint, data=data,
                    headers={'User-Agent': 'medical-literature-research/0.2'})
            with urllib.request.urlopen(req, timeout=60) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == 4:
                raise
            time.sleep(min(2 ** attempt, 8))


def search(term):
    body = request('esearch.fcgi', {'term': term, 'retmode': 'json',
                                   'retmax': 0, 'usehistory': 'y', 'sort': 'pub_date'})
    payload = json.loads(body)
    r = payload.get('esearchresult', {})
    if payload.get('error') or r.get('errorlist') or not r.get('querykey') or not r.get('webenv'):
        raise RuntimeError('PubMed ESearch returned an error or missing history: ' + str(payload)[:500])
    return {'count': int(r['count']), 'translation': r.get('querytranslation', ''),
            'query_key': r['querykey'], 'webenv': r['webenv']}


def parse_record(rec):
    cite = rec.find('./MedlineCitation')
    if cite is None:
        pmid = text(rec.find('./BookDocument/PMID'))
        return {'pmid': pmid, 'title': text(rec.find('./BookDocument/ArticleTitle')),
                'record_type': rec.tag, 'abstract': '', 'flags': 'book_record_check_manually'}
    art = cite.find('./Article')
    if art is None:
        raise RuntimeError('PubMed record is missing Article')
    pmid = text(cite.find('./PMID'))
    identifiers = {x.get('IdType', ''): text(x) for x in rec.findall('./PubmedData/ArticleIdList/ArticleId')}
    doi = identifiers.get('doi', '')
    if not doi:
        for x in art.findall('./ELocationID'):
            if x.get('EIdType') == 'doi':
                doi = text(x)
                break
    authors = []
    for a in art.findall('./AuthorList/Author'):
        last = text(a.find('./LastName'))
        first = text(a.find('./ForeName')) or text(a.find('./Initials'))
        name = text(a.find('./CollectiveName')) or ', '.join(filter(None, [last, first]))
        if name:
            authors.append(name)
    year = text(art.find('./Journal/JournalIssue/PubDate/Year'))
    if not year:
        year = text(art.find('./Journal/JournalIssue/PubDate/MedlineDate'))[:4]
    if not year:
        year = text(art.find('./ArticleDate/Year'))
    abstract = []
    for el in art.findall('./Abstract/AbstractText'):
        label = el.get('Label', '')
        abstract.append((label + ': ' if label else '') + text(el))
    types = [text(x) for x in art.findall('./PublicationTypeList/PublicationType')]
    links = [f"{x.get('RefType','')}:{text(x.find('./PMID'))}" for x in cite.findall('./CommentsCorrectionsList/CommentsCorrections')]
    flags = []
    if any('retract' in x.lower() or 'expression of concern' in x.lower() for x in types + links):
        flags.append('retraction_or_concern_check')
    if links:
        flags.append('related_correction_or_comment_check')
    return {'pmid': pmid, 'title': text(art.find('./ArticleTitle')),
            'authors': '; '.join(authors), 'year': year,
            'journal': text(art.find('./Journal/Title')), 'doi': doi,
            'pmcid': identifiers.get('pmc', ''), 'publication_types': '; '.join(types),
            'abstract': '\n'.join(abstract), 'record_type': rec.tag,
            'publication_status': text(rec.find('./PubmedData/PublicationStatus')),
            'related_notices': '; '.join(links), 'flags': '; '.join(flags),
            'url': 'https://pubmed.ncbi.nlm.nih.gov/' + pmid + '/'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--queries', required=True, help='JSON file: topic, queries [{name, term}], optional page_size')
    ap.add_argument('--out', required=True, help='new output folder (must not contain receipt.json)')
    ap.add_argument('--max-per-query', type=int, default=20000,
                    help='fail clearly before fetching a larger result; narrow or increase deliberately')
    args = ap.parse_args()
    config = json.loads(Path(args.queries).read_text(encoding='utf-8'))
    queries = config.get('queries', [])
    if not queries or len({q['name'] for q in queries}) != len(queries):
        raise ValueError('Supply at least one uniquely named query')
    page_size = int(config.get('page_size', 200))
    if not 1 <= page_size <= 500:
        raise ValueError('page_size must be 1..500')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'receipt.json').exists():
        raise FileExistsError('Output already contains a receipt; use a new directory')
    raw_dir = out / 'raw'
    raw_dir.mkdir(exist_ok=True)
    receipt = {'source': 'PubMed E-utilities', 'topic': config.get('topic', ''),
               'started_utc': now(), 'complete': False, 'queries': [], 'errors': [],
               'notes': ['Publication selection and reading are not performed by this script.',
                         'A count match describes retrieval for these queries, not search sensitivity or complete literature coverage.']}
    records = {}
    arms = defaultdict(set)
    try:
        for qindex, q in enumerate(queries, 1):
            initial = search(q['term'])
            qr = {'name': q['name'], 'query': q['term'], 'translation': initial['translation'],
                  'count': initial['count'], 'page_size': page_size, 'partitions': [], 'pages': [],
                  'retrieved_unique': 0, 'complete': False}
            receipt['queries'].append(qr)
            if initial['count'] > args.max_per_query:
                raise RuntimeError(f"{q['name']}: {initial['count']} > --max-per-query={args.max_per_query}; no claim of completion")
            def partition(term, s, lo=None, hi=None):
                if s['count'] <= MAX_ES:
                    qr['partitions'].append({'uid_low': lo, 'uid_high': hi, 'count': s['count']})
                    return [(term, s)]
                if lo is None:
                    lo, hi = 1, 999999999
                    allterm = f"({q['term']}) AND ({lo}:{hi}[UID])"
                    alls = search(allterm)
                    if alls['count'] != s['count']:
                        raise RuntimeError(f"{q['name']}: UID range does not cover original query count")
                    return partition(allterm, alls, lo, hi)
                if lo >= hi:
                    raise RuntimeError('Cannot split oversized UID segment')
                mid = (lo + hi) // 2
                lterm, rterm = f"({q['term']}) AND ({lo}:{mid}[UID])", f"({q['term']}) AND ({mid+1}:{hi}[UID])"
                ls, rs = search(lterm), search(rterm)
                if ls['count'] + rs['count'] != s['count']:
                    raise RuntimeError(f"{q['name']}: partition count changed; retry for a consistent snapshot")
                return partition(lterm, ls, lo, mid) + partition(rterm, rs, mid+1, hi)
            leaves = partition(q['term'], initial)
            seen = set()
            for pindex, (_term, p) in enumerate(leaves, 1):
                for start in range(0, p['count'], page_size):
                    n = min(page_size, p['count'] - start)
                    xml = request('efetch.fcgi', {'query_key': p['query_key'], 'WebEnv': p['webenv'],
                                                  'retstart': start, 'retmax': n, 'retmode': 'xml'})
                    rel = f'q{qindex:02d}-p{pindex:03d}-{start:07d}.xml'
                    (raw_dir / rel).write_bytes(xml)
                    root = ET.fromstring(xml)
                    children = [x for x in root if x.tag in ('PubmedArticle', 'PubmedBookArticle')]
                    if len(children) != n:
                        raise RuntimeError(f"{q['name']}: expected {n} records, received {len(children)} on page {start}")
                    pmids = []
                    for rec in children:
                        row = parse_record(rec)
                        pmid = row['pmid']
                        if not pmid or pmid in seen:
                            raise RuntimeError(f"{q['name']}: missing or duplicate PMID in pages: {pmid}")
                        seen.add(pmid)
                        pmids.append(pmid)
                        records[pmid] = row
                        arms[pmid].add(q['name'])
                    qr['pages'].append({'partition': pindex, 'retstart': start, 'requested': n,
                                        'received': len(pmids), 'file': 'raw/' + rel})
            qr['retrieved_unique'] = len(seen)
            if len(seen) != qr['count']:
                raise RuntimeError(f"{q['name']}: {len(seen)} retrieved vs {qr['count']} reported")
            qr['complete'] = True
            print(f"{q['name']}: {len(seen)}/{qr['count']} ({len(qr['pages'])} pages)", flush=True)
    except Exception as exc:
        receipt['errors'].append(f'{type(exc).__name__}: {exc}')
    finally:
        fields = ['pmid', 'title', 'authors', 'year', 'journal', 'doi', 'pmcid', 'publication_types',
                  'abstract', 'record_type', 'publication_status', 'related_notices', 'flags', 'url',
                  'query_arms', 'screening_status', 'screening_reason', 'reading_scope']
        with (out / 'candidates.csv').open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            w.writeheader()
            for pmid, row in records.items():
                row['query_arms'] = '; '.join(sorted(arms[pmid]))
                row['screening_status'] = '待筛选'
                row['screening_reason'] = ''
                row['reading_scope'] = '未阅读'
                w.writerow(row)
        receipt['finished_utc'] = now()
        receipt['unique_records'] = len(records)
        receipt['complete'] = len(receipt['queries']) == len(queries) and all(q['complete'] for q in receipt['queries']) and not receipt['errors']
        (out / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'complete': receipt['complete'], 'unique_records': len(records), 'errors': receipt['errors']}, ensure_ascii=False), flush=True)
    return 0 if receipt['complete'] else 1

if __name__ == '__main__':
    sys.exit(main())
