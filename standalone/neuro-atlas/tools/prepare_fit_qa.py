from pathlib import Path
import json
p=Path('standalone/neuro-atlas/evidence')
s=json.loads((p/'screenshots-preview.json').read_text(encoding='utf-8'))
s['capture']['after_each_step']=['accessibility']
s['actions']=s['actions'][:4]
s['actions'] += [dict(step_id='settled-mixed',action='wait_for_timeout',duration_ms=300),dict(step_id='regions-only',action='click',locator=dict(kind='css',selector='#labels')),dict(step_id='plain',action='click',locator=dict(kind='css',selector='#regions')),dict(step_id='details-only',action='click',locator=dict(kind='css',selector='#labels')),dict(step_id='mixed-again',action='click',locator=dict(kind='css',selector='#regions')),dict(step_id='settled',action='wait_for_timeout',duration_ms=300)]
s['environment']['viewport']={'width':1440,'height':900}
(p/'fit-layers-desktop.json').write_text(json.dumps(s,indent=2),encoding='utf-8')
s['environment']['viewport']={'width':390,'height':844}
(p/'fit-layers-mobile.json').write_text(json.dumps(s,indent=2),encoding='utf-8')
