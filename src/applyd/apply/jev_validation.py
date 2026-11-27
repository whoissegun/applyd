"""A successful, fresh per-job dry run is required for an opt-in live canary."""
import hashlib
import json
import re
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit


def application_url(url):
    parts=urlsplit(url)
    if parts.hostname=='jobs.ashbyhq.com' and len(parts.path.strip('/').split('/'))==2:
        return urlunsplit((parts.scheme,parts.netloc,parts.path.rstrip('/')+'/application','',''))
    return url


def input_fingerprint(profile, pdf_path):
    base=Path(__file__).parent
    digest=hashlib.sha256(json.dumps(profile,sort_keys=True).encode())
    digest.update(Path(pdf_path).read_bytes())
    for name in ('jev_controller.py','jev_validation.py','jev.py','runner.py','tools.py'):
        digest.update((base/name).read_bytes())
    return digest.hexdigest()


def validate_manifest(record, job_id, profile, pdf_path, url):
    """No secrets or candidate values appear in the manifest or errors."""
    try:
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(record['validated_at'])).total_seconds()
        schema=record['result']['jev_validated_schema']
        if (record['job_id']==job_id and record['test_mode'] is True
                and record['result']['note']=='test_mode:ready_for_review'
                and record['application_url']==application_url(url)
                and 0<=age<=3600 and isinstance(schema,str) and re.fullmatch(r'[a-f0-9]{64}',schema)
                and record['input_fingerprint']==input_fingerprint(profile,pdf_path)):
            return schema
    except (KeyError,TypeError,ValueError,OSError):
        pass
    return None
