#!/usr/bin/env python3
"""Apply explicitly reasoned human/agent decisions to retrieved PubMed records.

No automated relevance classification. The source candidate CSV is never changed.
"""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path

STATUSES = {'纳入重点', '排除', '待复核', '背景参考'}
SCOPES = {'题名', '摘要', '全文部分', '全文'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--candidates', required=True)
    ap.add_argument('--decisions', required=True, help='JSON list: pmid, status, reason, scope, optional study_family')
    ap.add_argument('--out', required=True, help='new review CSV')
    args = ap.parse_args()
    dest = Path(args.out)
    if dest.exists():
        raise FileExistsError('Review output exists; choose a new file')
    with Path(args.candidates).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        fields, rows = list(reader.fieldnames), list(reader)
    index = {r['pmid']: r for r in rows}
    if len(index) != len(rows):
        raise ValueError('Candidate PMIDs are not unique')
    decisions = json.loads(Path(args.decisions).read_text(encoding='utf-8'))
    if len({d['pmid'] for d in decisions}) != len(decisions):
        raise ValueError('Duplicate decision PMID')
    for d in decisions:
        pmid = d['pmid']
        if pmid not in index:
            raise ValueError(f'PMID absent from candidate file: {pmid}')
        if d.get('status') not in STATUSES or d.get('scope') not in SCOPES or not d.get('reason', '').strip():
            raise ValueError(f'Invalid decision for {pmid}: status, scope and reason required')
        if d['scope'] == '摘要' and not index[pmid]['abstract'].strip():
            raise ValueError(f'No abstract available for {pmid}; do not label abstract read')
        if d.get('review_priority', '') not in ('', '优先', '常规'):
            raise ValueError(f'Invalid review priority for {pmid}')
        index[pmid]['screening_status'] = d['status']
        index[pmid]['screening_reason'] = d['reason']
        index[pmid]['reading_scope'] = d['scope']
        index[pmid]['study_family'] = d.get('study_family', index[pmid].get('study_family', ''))
        index[pmid]['review_priority'] = d.get('review_priority', index[pmid].get('review_priority', ''))
    for name in ('study_family', 'review_priority'):
        if name not in fields:
            fields.append(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('x', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({'records': len(rows), 'decisions': len(decisions),
                      'status_counts': dict(Counter(r['screening_status'] for r in rows))}, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
