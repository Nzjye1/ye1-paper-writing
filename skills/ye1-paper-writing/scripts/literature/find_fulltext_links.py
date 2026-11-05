#!/usr/bin/env python3
"""Find candidate full-text links for public PMID/DOI identifiers using Europe PMC.

Links and open-access flags are leads, not evidence that a full text was retrieved,
read, or supports a claim. This script does not download the linked resources.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
DOI = re.compile(r'^10\.\d{4,9}/[-._;()/:A-Za-z0-9]+$', re.I)


def clean(item):
    pmid = str(item.get('pmid') or '').strip()
    doi = re.sub(r'^(https?://(dx\.)?doi\.org/|doi:\s*)', '',
                 str(item.get('doi') or '').strip(), flags=re.I)
    if pmid:
        if not pmid.isdigit() or len(pmid) > 12:
            raise ValueError('Invalid PMID')
        return {'pmid': pmid, 'doi': doi if DOI.fullmatch(doi) else ''}
    if not DOI.fullmatch(doi):
        raise ValueError('Each item requires a valid public PMID or DOI')
    return {'pmid': '', 'doi': doi}


def request(item):
    query = (f"EXT_ID:{item['pmid']} AND SRC:MED" if item['pmid']
             else f'DOI:"{item["doi"]}"')
    url = BASE + '?' + urllib.parse.urlencode({'query': query, 'format': 'json',
                                                'resultType': 'core', 'pageSize': '25'})
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'ye1-literature-research/0.3'})
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt or (isinstance(exc, urllib.error.HTTPError)
                           and exc.code not in (429, 500, 502, 503, 504)):
                raise
            time.sleep(1)


def candidates(payload, item):
    found = []
    for rec in (payload.get('resultList') or {}).get('result', []):
        if item['pmid'] and str(rec.get('pmid') or '') != item['pmid']:
            continue
        if not item['pmid'] and str(rec.get('doi') or '').casefold() != item['doi'].casefold():
            continue
        links = []
        for link in (rec.get('fullTextUrlList') or {}).get('fullTextUrl', []):
            url = link.get('url', '')
            if urllib.parse.urlparse(url).scheme not in ('https', 'http'):
                continue
            links.append({'url': url, 'site': link.get('site', ''),
                          'format': link.get('documentStyle', ''),
                          'availability': link.get('availability', ''),
                          'availability_code': link.get('availabilityCode', '')})
        found.append({'source': rec.get('source', ''), 'id': rec.get('id', ''),
                      'pmid': rec.get('pmid', ''), 'doi': rec.get('doi', ''),
                      'pmcid': rec.get('pmcid', ''), 'title': rec.get('title', ''),
                      'first_publication_date': rec.get('firstPublicationDate', ''),
                      'publication_types': (rec.get('pubTypeList') or {}).get('pubType', []),
                      'is_open_access': rec.get('isOpenAccess', ''), 'links': links})
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True, help='JSON: {"items": [{"pmid": "..."} or {"doi": "..."}]}')
    ap.add_argument('--out', required=True, help='new output JSON file')
    args = ap.parse_args()
    out, src = Path(args.out), Path(args.input)
    if out.exists() or out.resolve() == src.resolve():
        raise SystemExit('Output must be a new file; inputs are not overwritten.')
    items = json.loads(src.read_text(encoding='utf-8')).get('items', [])
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise SystemExit('Provide 1 to 50 public identifier items per run.')
    try:
        items = [clean(item) for item in items]
    except (AttributeError, ValueError) as exc:
        raise SystemExit(str(exc))
    results, errors = [], []
    for item in items:
        try:
            payload = request(item)
            results.append({'query': item, 'matched_records': candidates(payload, item),
                            'hit_count': payload.get('hitCount'), 'status': 'links_only_not_fetched'})
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
            errors.append({'query': item, 'kind': type(exc).__name__,
                           'http_status': getattr(exc, 'code', None)})
        time.sleep(0.25)
    result = {'source': 'Europe PMC REST API', 'retrieved_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'results': results, 'errors': errors,
              'note': 'URLs and availability labels are leads only, not downloaded or verified full text.'}
    with out.open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(json.dumps({'output': str(out), 'queries': len(results), 'errors': len(errors)}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
