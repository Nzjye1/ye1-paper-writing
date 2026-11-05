#!/usr/bin/env python3
"""Retrieve a bounded set of public PubMed records for manuscript references.

The caller chooses and reviews the search terms and candidate articles. Returned
abstracts are available material, not proof the source supports a manuscript claim.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

BASE = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
LAST = 0.0


def request(endpoint, params):
    global LAST
    params = dict(params, db='pubmed', tool='ye1_paper_writing')
    for envname, key in [('NCBI_EMAIL', 'email'), ('NCBI_API_KEY', 'api_key')]:
        if os.environ.get(envname):
            params[key] = os.environ[envname]
    payload = urllib.parse.urlencode(params).encode('utf-8')
    for attempt in range(2):
        delay = 0.38 - (time.monotonic() - LAST)
        if delay > 0:
            time.sleep(delay)
        LAST = time.monotonic()
        try:
            req = urllib.request.Request(BASE + endpoint, data=payload,
                                         headers={'User-Agent': 'ye1-paper-writing/1.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt or (isinstance(exc, urllib.error.HTTPError)
                           and exc.code not in (429, 500, 502, 503, 504)):
                raise
            time.sleep(1)


def joined(node):
    return ' '.join(''.join(node.itertext()).split()) if node is not None else ''


def parse_article(record):
    medline = record.find('./MedlineCitation')
    if medline is None:
        return None
    article = medline.find('./Article')
    if article is None:
        return None
    pmid = joined(medline.find('./PMID'))
    ids = {item.get('IdType'): joined(item)
           for item in record.findall('./PubmedData/ArticleIdList/ArticleId')}
    if not ids.get('doi'):
        ids['doi'] = next((joined(x) for x in article.findall('./ELocationID')
                           if x.get('EIdType') == 'doi'), '')
    authors = []
    for author in article.findall('./AuthorList/Author'):
        corporate = joined(author.find('./CollectiveName'))
        family = joined(author.find('./LastName'))
        initials = joined(author.find('./Initials'))
        if corporate or family:
            authors.append(corporate or ' '.join(filter(None, [family, initials])))
    date = article.find('./Journal/JournalIssue/PubDate')
    year = joined(date.find('./Year')) if date is not None else ''
    if not year and date is not None:
        year = joined(date.find('./MedlineDate'))[:4]
    abstract = []
    for section in article.findall('./Abstract/AbstractText'):
        label = section.get('Label', '')
        body = joined(section)
        if body:
            abstract.append((label + ': ' if label else '') + body)
    kinds = [joined(item) for item in article.findall('./PublicationTypeList/PublicationType')]
    notices = [{'type': item.get('RefType', ''), 'pmid': joined(item.find('./PMID')),
                'note': joined(item.find('./Note'))}
               for item in medline.findall('./CommentsCorrectionsList/CommentsCorrections')]
    pages = joined(article.find('./Pagination/MedlinePgn')) or joined(article.find('./Pagination/StartPage'))
    return {'pmid': pmid, 'doi': ids.get('doi', ''), 'pmcid': ids.get('pmc', ''),
            'title': joined(article.find('./ArticleTitle')), 'authors': authors,
            'journal': joined(article.find('./Journal/Title')),
            'journal_abbreviation': joined(article.find('./Journal/ISOAbbreviation')),
            'year': year, 'volume': joined(article.find('./Journal/JournalIssue/Volume')),
            'issue': joined(article.find('./Journal/JournalIssue/Issue')), 'pages': pages,
            'publication_types': kinds, 'related_notices': notices,
            'abstract': '\n'.join(abstract), 'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
            'available_material': 'metadata_and_abstract' if abstract else 'metadata_only',
            'selection_status': 'not_screened'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queries', required=True, help='JSON with queries [{name, term}]')
    parser.add_argument('--out', required=True, help='new JSON output file')
    parser.add_argument('--limit', type=int, default=40, help='records fetched per query, 1..100')
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        raise SystemExit('limit must be 1..100')
    out = Path(args.out)
    if out.exists() or out.resolve() == Path(args.queries).resolve():
        raise SystemExit('Output must be a new file; inputs are not overwritten.')
    cfg = json.loads(Path(args.queries).read_text(encoding='utf-8'))
    queries = cfg.get('queries', [])
    if not queries or len({q.get('name') for q in queries}) != len(queries):
        raise SystemExit('Each query must have a unique name and a search term.')
    if any(not isinstance(q.get('term'), str) or not q['term'].strip() or not q.get('name') for q in queries):
        raise SystemExit('Each query must have a unique name and a search term.')
    results, receipt, errors = {}, [], []
    for query in queries:
        item = {'name': query['name'], 'term': query['term'], 'matched': None,
                'requested': 0, 'retrieved': 0, 'partial_sample': False}
        try:
            payload = json.loads(request('esearch.fcgi', {'term': query['term'], 'retmode': 'json',
                                                           'retmax': args.limit, 'sort': 'relevance'}))
            if payload.get('error') or payload.get('esearchresult', {}).get('errorlist'):
                raise ValueError('PubMed rejected the query')
            search = payload['esearchresult']
            ids = search.get('idlist', [])
            item['matched'] = int(search['count'])
            item['requested'] = len(ids)
            item['partial_sample'] = item['matched'] > len(ids)
            if ids:
                xml = ET.fromstring(request('efetch.fcgi', {'id': ','.join(ids), 'retmode': 'xml'}))
                fetched = set()
                for source in xml.findall('./PubmedArticle'):
                    rec = parse_article(source)
                    if rec and rec['pmid']:
                        fetched.add(rec['pmid'])
                        prior = results.setdefault(rec['pmid'], dict(rec, matched_queries=[]))
                        if query['name'] not in prior['matched_queries']:
                            prior['matched_queries'].append(query['name'])
                item['retrieved'] = len(fetched)
                if set(ids) != fetched:
                    item['warning'] = 'Some returned IDs could not be parsed; inspect source results.'
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, ET.ParseError) as exc:
            errors.append({'query': query['name'], 'kind': type(exc).__name__,
                           'http_status': getattr(exc, 'code', None)})
        receipt.append(item)
    result = {'source': 'PubMed E-utilities', 'retrieved_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'queries': receipt, 'errors': errors, 'records': list(results.values()),
              'note': 'Bounded candidates, not a systematic search. Retrieved abstracts are not yet read or judged.'}
    with out.open('x', encoding='utf-8') as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write('\n')
    print(json.dumps({'output': str(out), 'unique_records': len(results), 'errors': len(errors),
                      'partial_queries': sum(q['partial_sample'] for q in receipt)}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
