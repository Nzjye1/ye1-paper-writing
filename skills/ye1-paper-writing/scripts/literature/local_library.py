#!/usr/bin/env python3
"""Maintain an inspectable local topic library without requiring Zotero.

Automatic full-text retrieval uses PMC's article dataset on the public S3
service. It does not scrape the PMC website or bypass subscriptions.
"""
import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BUCKET = 'https://pmc-oa-opendata.s3.amazonaws.com'
TOPIC_META = '主题.json'
CATALOG = '文献清单.csv'
INDEX = '文献索引_自动生成.md'
LIBRARY_INDEX = '研究方向索引_自动生成.md'
FULLTEXT = '全文'
MAX_BYTES = 50 * 1024 * 1024
LAST_REQUEST = 0.0
REVIEW_FIELDS = {'screening_status', 'screening_reason', 'reading_scope',
                 'study_family', 'review_priority', '本次全文获取状态', '本次全文内容复核状态'}
FULLTEXT_FIELDS = ['fulltext_status', 'fulltext_path', 'fulltext_source',
                   'fulltext_license', 'fulltext_version', 'fulltext_checked_utc', 'fulltext_note']
BASE_FIELDS = ['record_key', 'pmid', 'doi', 'local_id', 'title', 'authors', 'year', 'journal', 'pmcid', 'url',
               'abstract', 'publication_types', 'publication_status', 'related_notices',
               'flags', 'query_arms', 'screening_status', 'screening_reason', 'reading_scope',
               'study_family', 'review_priority'] + FULLTEXT_FIELDS


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.library-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(topic):
    topic = Path(topic)
    if not (topic / TOPIC_META).is_file():
        raise ValueError('主题未初始化：' + str(topic))
    lock = topic / '.文献库.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RuntimeError('主题正在更新，或异常退出后锁文件需检查：' + str(lock))
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def doi_norm(value):
    return re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', (value or '').strip(), flags=re.I).lower()


def record_key(row):
    pmid = (row.get('pmid') or '').strip()
    if pmid:
        if not pmid.isdigit():
            raise ValueError('PMID 必须为数字')
        return 'PMID:' + pmid
    doi = doi_norm(row.get('doi', ''))
    if re.fullmatch(r'10\.\d{4,9}/\S+', doi):
        return 'DOI:' + doi
    local_id = (row.get('local_id') or '').strip()
    if re.fullmatch(r'[A-Za-z0-9_-]{1,80}', local_id):
        return 'LOCAL:' + local_id
    raise ValueError('题录需要 PMID、有效 DOI 或 local_id')


def read_catalog(topic):
    path = Path(topic) / CATALOG
    if not path.exists():
        return list(BASE_FIELDS), []
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        fields, rows = list(reader.fieldnames or []), list(reader)
    keys = [record_key(r) for r in rows]
    if len(set(keys)) != len(rows):
        raise ValueError('文献清单存在重复文献标识')
    for row, key in zip(rows, keys):
        row['record_key'] = key
    for name in BASE_FIELDS:
        if name not in fields:
            fields.append(name)
    return fields, rows


def md(value):
    return str(value or '').replace('\\', '\\\\').replace('|', '\\|').replace('\n', ' ').replace('[', '\\[').replace(']', '\\]')


def relative_link(value):
    return urllib.parse.quote(str(value).replace('\\', '/'), safe='/')


def write_library_index(root):
    root = Path(root)
    entries = []
    for meta in sorted(root.glob('*/' + TOPIC_META)):
        try:
            data = json.loads(meta.read_text(encoding='utf-8'))
            entries.append((data.get('name', meta.parent.name), meta.parent.name))
        except (OSError, ValueError):
            continue
    lines = ['# 医学文献库：研究方向', '', '这是自动生成的目录；个人笔记请放在各主题文件夹中。', '']
    for name, folder in entries:
        lines.append('- [' + md(name) + '](' + relative_link(folder + '/' + INDEX) + ')')
    atomic_write(root / LIBRARY_INDEX, '\n'.join(lines) + '\n')


def write_catalog_and_index(topic, fields, rows):
    topic = Path(topic)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(topic / CATALOG, '\ufeff' + stream.getvalue())
    info = json.loads((topic / TOPIC_META).read_text(encoding='utf-8'))
    counts = Counter(r.get('fulltext_status') or '未尝试' for r in rows)
    links = '[文献清单 CSV](' + relative_link(CATALOG) + ') · [检索与追踪](检索与追踪/) · [我的笔记](我的笔记.md)'
    reports = sorted(topic.glob('调研报告*.md'))
    if reports:
        report_links = ' · '.join('[' + md(p.stem) + '](' + relative_link(p.name) + ')' for p in reports)
        links = report_links + ' · ' + links
    lines = ['# ' + md(info.get('name', topic.name)), '',
             '这是自动生成的文献目录。全文已保存不等于已阅读；查看筛选细节请打开文献清单。', '',
             '文献数：' + str(len(rows)) + '；全文状态：' + '，'.join(f'{k} {v}' for k, v in sorted(counts.items())), '',
             links, '',
             '| 文献 | 筛选状态 | 全文 | 实际阅读范围 |', '| --- | --- | --- | --- |']
    priority = {'纳入重点': 0, '背景参考': 1, '待复核': 2, '待筛选': 3, '排除': 4}
    for row in sorted(rows, key=lambda r: (priority.get(r.get('screening_status', ''), 5),
                                            str(r.get('year', '')), record_key(r)), reverse=False):
        title = md(row.get('title') or '未提供题名')
        url = ('https://pubmed.ncbi.nlm.nih.gov/' + row['pmid'] + '/' if row.get('pmid') else
               'https://doi.org/' + urllib.parse.quote(doi_norm(row.get('doi', '')), safe='/') if row.get('doi') else
               row.get('url', ''))
        paper = '[' + title + '](' + url + ')' if urllib.parse.urlparse(url).scheme in ('https', 'http') else title
        full = md(row.get('fulltext_status') or '未尝试')
        path = row.get('fulltext_path', '')
        if path and (topic / path).is_file():
            full = '[' + full + '](' + relative_link(path) + ')'
        lines.append('| ' + paper + ' (' + md(row.get('year', '')) + ') | ' +
                     md(row.get('screening_status') or '待筛选') + ' | ' + full + ' | ' +
                     md(row.get('reading_scope') or '未阅读') + ' |')
    atomic_write(topic / INDEX, '\n'.join(lines) + '\n')
    write_library_index(topic.parent)


def init_topic(topic, name):
    topic = Path(topic)
    if not name.strip():
        raise ValueError('主题名称不能为空')
    if topic.exists() and any(topic.iterdir()):
        raise FileExistsError('目标目录非空，避免覆盖：' + str(topic))
    topic.mkdir(parents=True, exist_ok=True)
    (topic / FULLTEXT).mkdir(exist_ok=True)
    (topic / '检索与追踪').mkdir(exist_ok=True)
    (topic / TOPIC_META).write_text(json.dumps({'name': name, 'created_utc': now(), 'version': 1},
                                               ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (topic / '我的笔记.md').write_text('# 我的笔记\n\n', encoding='utf-8')
    write_catalog_and_index(topic, list(BASE_FIELDS), [])
    return {'topic': str(topic), 'name': name, 'created': True}


def import_candidates(topic, source, update_screening=False):
    topic = Path(topic)
    with Path(source).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        incoming_fields, incoming = list(reader.fieldnames or []), list(reader)
    incoming_keys = [record_key(r) for r in incoming]
    if len(set(incoming_keys)) != len(incoming):
        raise ValueError('导入文件存在重复文献标识')
    with locked(topic):
        fields, rows = read_catalog(topic)
        for name in incoming_fields:
            if name not in fields and name not in FULLTEXT_FIELDS:
                fields.append(name)
        index = {record_key(r): r for r in rows}
        added = updated = conflicts = 0
        for new, key in zip(incoming, incoming_keys):
            if key not in index:
                row = {k: v or '' for k, v in new.items() if k not in FULLTEXT_FIELDS}
                row['record_key'] = key
                row['fulltext_status'] = '未尝试'
                rows.append(row)
                index[key] = row
                added += 1
                continue
            old = index[key]
            conflict = bool(old.get('doi') and new.get('doi') and doi_norm(old['doi']) != doi_norm(new['doi']))
            if conflict:
                conflicts += 1
                old['flags'] = '; '.join(filter(None, [old.get('flags', ''), '同 PMID DOI 不一致，需人工核对']))
            for key, value in new.items():
                if not value or key in FULLTEXT_FIELDS or key in ('pmid', 'record_key') or (key == 'doi' and conflict):
                    continue
                if key in REVIEW_FIELDS and not update_screening:
                    continue
                if key == 'flags' and conflict:
                    continue
                old[key] = value
            updated += 1
        write_catalog_and_index(topic, fields, rows)
        return {'added': added, 'updated': updated, 'doi_conflicts': conflicts, 'total': len(rows)}


def request_bytes(url, limit=MAX_BYTES):
    global LAST_REQUEST
    for attempt in range(3):
        delay = 0.25 - (time.monotonic() - LAST_REQUEST)
        if delay > 0:
            time.sleep(delay)
        LAST_REQUEST = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'medical-literature-research-local-library/0.3'})
            with urllib.request.urlopen(req, timeout=40) as response:
                length = response.headers.get('Content-Length')
                if length and int(length) > limit:
                    raise ValueError('文件超过大小上限')
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError('文件超过大小上限')
                return data
        except (urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def list_pmc_metadata(pmcid):
    if not re.fullmatch(r'PMC[0-9]+', pmcid):
        raise ValueError('无效 PMCID')
    params = {'list-type': '2', 'prefix': 'metadata/' + pmcid + '.', 'max-keys': '100'}
    versions = []
    while True:
        data = request_bytes(BUCKET + '/?' + urllib.parse.urlencode(params), limit=1024 * 1024)
        root = ET.fromstring(data)
        for el in root.findall('{*}Contents/{*}Key'):
            match = re.fullmatch(r'metadata/' + pmcid + r'\.([0-9]+)\.json', el.text or '')
            if match:
                versions.append((int(match.group(1)), el.text))
        if root.findtext('{*}IsTruncated') != 'true':
            break
        token = root.findtext('{*}NextContinuationToken')
        if not token:
            raise RuntimeError('PMC 版本列表分页不完整')
        params['continuation-token'] = token
    if not versions:
        return None
    version, key = max(versions)
    data = json.loads(request_bytes(BUCKET + '/' + urllib.parse.quote(key, safe='/'), limit=1024 * 1024))
    if data.get('pmcid') != pmcid or int(data.get('version', -1)) != version:
        raise ValueError('PMC 元数据身份或版本不一致')
    return data


def s3_url(value, pmcid, version, extension):
    parsed = urllib.parse.urlparse(value)
    expected = f'/{pmcid}.{version}/{pmcid}.{version}.{extension}'
    if parsed.scheme != 's3' or parsed.netloc != 'pmc-oa-opendata' or parsed.path != expected:
        raise ValueError('PMC 文件地址与题录身份不一致')
    checksum = urllib.parse.parse_qs(parsed.query).get('md5', [''])[0]
    if checksum and not re.fullmatch(r'[a-fA-F0-9]{32}', checksum):
        raise ValueError('无效全文校验值')
    return BUCKET + urllib.parse.quote(parsed.path, safe='/'), checksum.lower()


def valid_document(data, kind):
    if not data:
        return False
    if kind == 'pdf':
        return data.startswith(b'%PDF-')
    if kind == 'xml':
        try:
            root = ET.fromstring(data)
            kind = root.tag.split('}')[-1]
            bodies = root.findall('.//{*}body')
            return kind in ('article', 'article-set', 'pmc-articleset') and any(''.join(b.itertext()).strip() for b in bodies)
        except ET.ParseError:
            return False
    return not data.lstrip().lower().startswith((b'<!doctype html', b'<html'))


def document_basename(row):
    key = record_key(row)
    if key.startswith('PMID:'):
        return 'PMID_' + key[5:]
    return key.split(':', 1)[0] + '_' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:20]


def fetch_one(topic, row):
    topic = Path(topic)
    previous = row.get('fulltext_path', '')
    if previous:
        resolved = (topic / previous).resolve()
        if not resolved.is_relative_to(topic.resolve()):
            raise ValueError('已有全文路径不在主题目录内')
        if resolved.is_file():
            return '已保存'
    row['fulltext_checked_utc'] = now()
    pmcid = (row.get('pmcid') or '').strip().upper()
    if not pmcid:
        row['fulltext_status'] = '待其他来源查找'
        row['fulltext_note'] = '题录无 PMCID；尚未检查其他开放或用户授权来源。'
        return row['fulltext_status']
    try:
        meta = list_pmc_metadata(pmcid)
        if meta is None:
            row['fulltext_status'] = 'PMC未找到开放文件'
            row['fulltext_note'] = '未在 PMC 公开数据集中找到；不代表其他来源不可获取。'
            return row['fulltext_status']
        if row.get('pmid') and meta.get('pmid') and str(meta['pmid']) != row['pmid']:
            raise ValueError('PMC 元数据与题录 PMID 不一致')
        if not row.get('pmid') and row.get('doi') and meta.get('doi') and doi_norm(row['doi']) != doi_norm(meta['doi']):
            raise ValueError('PMC 元数据与题录 DOI 不一致')
        if meta.get('is_retracted'):
            row['flags'] = '; '.join(filter(None, [row.get('flags', ''), 'PMC 标记撤稿，需复核']))
        row['fulltext_license'] = str(meta.get('license_code') or '未标明')
        row['fulltext_version'] = str(meta.get('version', ''))
        errors = []
        for key, kind in [('pdf_url', 'pdf'), ('text_url', 'txt'), ('xml_url', 'xml')]:
            if not meta.get(key):
                continue
            try:
                url, checksum = s3_url(meta[key], pmcid, meta['version'], kind)
                data = request_bytes(url)
                if not valid_document(data, kind):
                    raise ValueError('下载文件格式不符')
                if checksum and hashlib.md5(data).hexdigest() != checksum:
                    raise ValueError('全文校验值不匹配')
                dest = topic / FULLTEXT / (document_basename(row) + '.' + kind)
                if dest.exists():
                    if dest.read_bytes() != data:
                        raise FileExistsError('目标文件已存在但内容不同，未覆盖')
                else:
                    created = False
                    try:
                        f = dest.open('xb')
                        created = True
                        with f:
                            f.write(data)
                    except OSError:
                        if created:
                            dest.unlink(missing_ok=True)
                        raise
                row['fulltext_status'] = '已保存 ' + kind.upper()
                row['fulltext_path'] = str(dest.relative_to(topic))
                row['fulltext_source'] = url
                row['fulltext_note'] = ('PMC 公开数据集；' + ('作者手稿版本；' if meta.get('is_manuscript') else '') +
                                        '仅主文，补充材料未自动保存。' + (' 其他格式失败：' + '; '.join(errors) if errors else ''))
                return row['fulltext_status']
            except (OSError, ValueError, urllib.error.URLError) as exc:
                errors.append(kind + ': ' + str(exc)[:180])
        row['fulltext_status'] = '获取失败' if errors else 'PMC未提供全文文件'
        row['fulltext_note'] = '; '.join(errors) if errors else 'PMC 元数据没有 PDF、文本或 XML 文件。'
    except (OSError, ValueError, RuntimeError, ET.ParseError, urllib.error.URLError) as exc:
        row['fulltext_status'] = '获取失败'
        row['fulltext_note'] = type(exc).__name__ + ': ' + str(exc)[:180]
    return row['fulltext_status']


def fetch_fulltexts(topic, pmids=None, selection='relevant', limit=None, retry=False, ids=None):
    topic = Path(topic)
    with locked(topic):
        fields, rows = read_catalog(topic)
        if pmids is not None and ids is not None:
            raise ValueError('不能同时指定 PMID 和其他标识')
        requested = ({'PMID:' + p for p in pmids} if pmids is not None else
                     set(ids) if ids is not None else None)
        if requested is not None and not requested.issubset({record_key(r) for r in rows}):
            raise ValueError('指定文献不在清单中')
        if requested is not None:
            selected = [r for r in rows if record_key(r) in requested]
        else:
            selected = [r for r in rows if (selection == 'all' or r.get('screening_status') != '排除')
                        and (retry or (r.get('fulltext_status') or '未尝试') == '未尝试')]
            if retry:
                selected = [r for r in selected if not (r.get('fulltext_path') and (topic / r['fulltext_path']).is_file())]
        if limit is not None:
            selected = selected[:limit]
        result = Counter()
        for i, row in enumerate(selected, 1):
            result[fetch_one(topic, row)] += 1
            write_catalog_and_index(topic, fields, rows)
            print(f"[{i}/{len(selected)}] {record_key(row)}: {row.get('fulltext_status', '')}", flush=True)
        return {'attempted_or_existing': len(selected), 'results': dict(result)}


def attach(topic, identifier, file_path):
    topic, source = Path(topic), Path(file_path)
    if source.suffix.lower() not in ('.pdf', '.txt', '.xml'):
        raise ValueError('只接受 PDF、TXT 或 XML 全文')
    if source.stat().st_size > MAX_BYTES:
        raise ValueError('文件超过大小上限')
    data = source.read_bytes()
    kind = source.suffix.lower().lstrip('.')
    if not valid_document(data, kind):
        raise ValueError('文件格式不符合扩展名')
    with locked(topic):
        fields, rows = read_catalog(topic)
        key = ('PMID:' + identifier if identifier.isdigit() else identifier if identifier.startswith('LOCAL:') else
               'DOI:' + doi_norm(identifier.removeprefix('DOI:')))
        matches = [r for r in rows if record_key(r) == key]
        if len(matches) != 1:
            raise ValueError('指定文献不在清单中')
        row = matches[0]
        existing = row.get('fulltext_path', '')
        if existing and (topic / existing).is_file():
            raise FileExistsError('该文献已有全文，未覆盖')
        dest = topic / FULLTEXT / (document_basename(row) + '.' + kind)
        created = False
        try:
            f = dest.open('xb')
            created = True
            with f:
                f.write(data)
        except OSError:
            if created:
                dest.unlink(missing_ok=True)
            raise
        row.update({'fulltext_status': '已保存 ' + kind.upper(), 'fulltext_path': str(dest.relative_to(topic)),
                    'fulltext_source': '用户提供', 'fulltext_license': '由用户确认',
                    'fulltext_checked_utc': now(), 'fulltext_note': '用户提供的文件；需核对论文身份。'})
        write_catalog_and_index(topic, fields, rows)
        return {'record_key': key, 'path': str(dest)}


def search(root, term):
    query = term.casefold().strip()
    if not query:
        raise ValueError('请输入搜索词')
    hits = []
    for path in sorted(Path(root).glob('*/' + CATALOG)):
        _, rows = read_catalog(path.parent)
        for row in rows:
            if query in ' '.join(row.get(k, '') for k in ('pmid', 'title', 'authors', 'year', 'doi')).casefold():
                hits.append({'topic': str(path.parent), 'record_key': record_key(row), 'pmid': row.get('pmid', ''), 'title': row.get('title', ''),
                             'fulltext_status': row.get('fulltext_status', '未尝试'),
                             'fulltext_path': str(path.parent / row['fulltext_path']) if row.get('fulltext_path') else ''})
    return hits


def reindex(topic):
    with locked(topic):
        fields, rows = read_catalog(topic)
        write_catalog_and_index(topic, fields, rows)
        return {'total': len(rows), 'index': str(Path(topic) / INDEX)}


def main():
    ap = argparse.ArgumentParser(description='按研究方向保存本地文献、索引和 PMC 开放全文')
    sub = ap.add_subparsers(dest='command', required=True)
    a = sub.add_parser('init'); a.add_argument('--topic', required=True); a.add_argument('--name', required=True)
    b = sub.add_parser('import'); b.add_argument('--topic', required=True); b.add_argument('--candidates', required=True); b.add_argument('--update-screening', action='store_true')
    c = sub.add_parser('fetch'); c.add_argument('--topic', required=True)
    group = c.add_mutually_exclusive_group(required=True)
    group.add_argument('--pmids', help='逗号分隔的 PMID'); group.add_argument('--ids', help='逗号分隔的 PMID:... 或 DOI:... 标识'); group.add_argument('--relevant', action='store_true'); group.add_argument('--all', action='store_true')
    c.add_argument('--limit', type=int); c.add_argument('--retry', action='store_true', help='重试未保存全文的条目')
    d = sub.add_parser('attach'); d.add_argument('--topic', required=True); dg = d.add_mutually_exclusive_group(required=True); dg.add_argument('--pmid'); dg.add_argument('--doi'); dg.add_argument('--local-id'); d.add_argument('--file', required=True)
    e = sub.add_parser('search'); e.add_argument('--root', required=True); e.add_argument('--term', required=True)
    f = sub.add_parser('reindex'); f.add_argument('--topic', required=True)
    args = ap.parse_args()
    if args.command == 'init': result = init_topic(args.topic, args.name)
    elif args.command == 'import': result = import_candidates(args.topic, args.candidates, args.update_screening)
    elif args.command == 'fetch':
        if args.limit is not None and args.limit < 1: raise ValueError('limit 必须为正数')
        pmids = [x.strip() for x in args.pmids.split(',') if x.strip()] if args.pmids is not None else None
        if pmids is not None and (not pmids or any(not x.isdigit() for x in pmids)):
            raise ValueError('请提供至少一个数字 PMID')
        ids = [x.strip() for x in args.ids.split(',') if x.strip()] if args.ids is not None else None
        if ids is not None and not ids:
            raise ValueError('请提供至少一个文献标识')
        result = fetch_fulltexts(args.topic, pmids, 'all' if args.all else 'relevant', args.limit, args.retry, ids)
    elif args.command == 'attach': result = attach(args.topic, args.pmid or args.doi or 'LOCAL:' + args.local_id, args.file)
    elif args.command == 'search': result = search(args.root, args.term)
    else: result = reindex(args.topic)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError) as exc:
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        sys.exit(1)
