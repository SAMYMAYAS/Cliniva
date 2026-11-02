import json
from pathlib import Path
p=Path('standalone/neuro-atlas/evidence')
for size in ['desktop','mobile']:
 r=json.loads((p/f'fit-layers-{size}-result.json').read_text(encoding='utf-8'))
 steps={s['step_id']:s for s in r['normalized_result']['steps']}
 for key,detail,region in [('settled-mixed',True,True),('regions-only',False,True),('plain',False,False),('details-only',True,False),('mixed-again',True,True)]:
  t=steps[key]['artifacts']['accessibility']['value']['text']
  assert ('button "Gyrus frontal supérieur' in t)==detail,(size,key,'detail')
  assert ('button "Lobe frontal' in t)==region,(size,key,'region')
 print(size,'5 combinaisons vérifiées')
