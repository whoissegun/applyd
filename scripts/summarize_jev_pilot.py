"""Read a batch and its traces; print non-PII hybrid coverage and cost totals."""
import argparse
import json
from pathlib import Path

from applyd.local_store import get_local_store


def summarize(path):
    report = json.loads(Path(path).read_text())
    store = get_local_store()
    summary = {'finished': bool(report.get('finished_at')), 'requested': report['requested'],
               'jobs': [], 'kimi_apply_cost_usd': report.get('apply_cost_usd', 0),
               'jev_estimated_cost_usd': 0, 'jev_requests': 0,
               'jev_tool_successes': 0, 'jev_tool_errors': 0, 'kimi_turns': 0}
    for row in report['results']:
        item = {'company': row['company'], 'title': row['title'],
                'status': row.get('final_status', row.get('status')),
                'reason': row.get('final_reason', row.get('reason')), 'jev_requests': 0,
                'jev_tool_successes': 0, 'jev_tool_errors': 0,
                'kimi_turns': 0, 'jev_estimated_cost_usd': 0}
        for attempt in (row.get('local'), row.get('brightdata')):
            if not attempt:
                continue
            events = store.get_apply_trace(attempt['id'])
            jev_turns = {e['turn'] for e in events if e['event_type'] == 'model_turn'
                         and e['payload'].get('provider') == 'jev_hybrid'}
            for event in events:
                payload = event['payload']
                if event['event_type'] == 'jev_decision':
                    if payload.get('model'):
                        item['jev_requests'] += 1
                        item['jev_estimated_cost_usd'] += payload.get('estimated_cost_usd', 0)
                elif event['event_type'] == 'model_turn' and payload.get('provider') == 'kimi':
                    item['kimi_turns'] += 1
                elif event['event_type'] == 'tool_result' and event['turn'] in jev_turns:
                    result = str(payload.get('result', ''))
                    item['jev_tool_successes'] += result.startswith('ok:')
                    item['jev_tool_errors'] += result.startswith('error:')
        for key in ('jev_requests', 'jev_estimated_cost_usd', 'jev_tool_successes',
                    'jev_tool_errors', 'kimi_turns'):
            summary[key] += item[key]
        summary['jobs'].append(item)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report')
    args = parser.parse_args()
    print(json.dumps(summarize(args.report), indent=2))
