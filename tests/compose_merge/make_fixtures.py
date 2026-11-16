"""Authoring helper for the checked synthetic fixture manifest; not a test oracle."""
import json
from pathlib import Path

cases = []

def case(name, base, later, expected, path, action, winner=1):
    cases.append(dict(name=name, files=[base, later], expected=expected, path=path, action=action, winner=winner))

def svc(body):
    return 'services:\n  web:\n    image: example.invalid/web:1\n' + ''.join('    ' + line + '\n' for line in body.splitlines())

def service(value):
    return {'services': {'web': {'image': 'example.invalid/web:1', **value}}}

case('map',svc('labels: {keep: base, change: old}'),svc('labels: {change: new, added: yes}'),service({'labels':{'keep':'base','change':'new','added':'yes'}}),'/services/web/labels/change','replace')
case('append',svc('dns: [1.1.1.1]'),svc('dns: [8.8.8.8]'),service({'dns':['1.1.1.1','8.8.8.8']}),'/services/web/dns','append')
for field in ['command','entrypoint']:
    case(field,svc(field+': [echo, old]'),svc(field+': [echo, new]'),service({field:['echo','new']}),'/services/web/'+field,'replace')
case('healthcheck',svc('healthcheck: {test: [CMD, echo, old], interval: 30s}'),svc('healthcheck: {test: [CMD, echo, new]}'),service({'healthcheck':{'test':['CMD','echo','new'],'interval':'30s'}}),'/services/web/healthcheck/test','replace')
case('command-null',svc('command: [echo, old]'),svc('command: null'),service({'command':None}),'/services/web/command','replace')
case('command-string',svc('command: [echo, old]'),svc('command: echo new'),service({'command':'echo new'}),'/services/web/command','replace')
case('volume',svc('volumes: ["cache:/cache:ro"]'),svc('volumes: [{type: volume, source: fresh, target: /cache, read_only: false}]'),service({'volumes':[{'type':'volume','source':'fresh','target':'/cache','read_only':False}]}),'/services/web/volumes/0/source','replace')
# External resources make config-only oracle validation independent of the filesystem.
resources='secrets:\n  token: {external: true}\n  newer: {external: true}\nconfigs:\n  settings: {external: true}\n  newer: {external: true}\nvolumes:\n  cache: {}\n  fresh: {}\n'
for c in cases:
    if c['name']=='volume':
        c['files']=[f+resources for f in c['files']]
        c['expected'].update({'secrets':{'token':{'external':True},'newer':{'external':True}},'configs':{'settings':{'external':True},'newer':{'external':True}},'volumes':{'cache':{},'fresh':{}}})
for kind,source,target in [('secrets','token','/run/secrets/token'),('configs','settings','/settings')]:
    case(kind,svc(f'{kind}: [{source}]')+resources,svc(f'{kind}: [{{source: newer, target: {target}}}]'),{**service({kind:[{'source':'newer','target':target}]}),'secrets':{'token':{'external':True},'newer':{'external':True}},'configs':{'settings':{'external':True},'newer':{'external':True}},'volumes':{'cache':{},'fresh':{}}},f'/services/web/{kind}/0/source','replace')
port={'target':80,'published':'8080','protocol':'tcp','mode':'ingress'}
case('port-shared',svc('ports: ["8080:80"]'),svc('ports: [{target: 80, published: "8080", name: public}]'),service({'ports':[{**port,'name':'public'}]}),'/services/web/ports/0/name','add')
for name,later,p in [
    ('ip','127.0.0.1:8080:80',{**port,'host_ip':'127.0.0.1'}),
    ('target','8080:81',{**port,'target':81}),
    ('published','8081:80',{**port,'published':'8081'}),
    ('protocol','8080:80/udp',{**port,'protocol':'udp'}),
]:
    case('port-'+name,svc('ports: ["8080:80"]'),svc(f'ports: ["{later}"]'),service({'ports':[port,p]}),'/services/web/ports/1','add')
case('port-default-ip',svc('ports: ["8080:80"]'),svc('ports: ["0.0.0.0:8080:80"]'),service({'ports':[{**port,'host_ip':'0.0.0.0'}]}),'/services/web/ports/0/host_ip','add')
case('port-unquoted',svc('ports: [1:20]'),svc('ports: ["1:20"]'),service({'ports':[{'target':20,'published':'1','protocol':'tcp','mode':'ingress'}]}),'/services/web/ports','merge')
case('reset-list',svc('dns: [1.1.1.1]'),svc('dns: !reset []'),service({}),'/services/web/dns','reset')
case('reset-scalar',svc('environment: {REMOVE: old, KEEP: base}'),svc('environment: {REMOVE: !reset null}'),service({'environment':{'KEEP':'base'}}),'/services/web/environment/REMOVE','reset')
case('reset-map',svc('labels: {old: value}'),svc('labels: !reset {}'),service({}),'/services/web/labels','reset')
case('override-list',svc('ports: ["8080:80"]'),svc('ports: !override ["9443:443"]'),service({'ports':[{'target':443,'published':'9443','protocol':'tcp','mode':'ingress'}]}),'/services/web/ports','override')
case('override-map',svc('environment: {old: value, keep: base}'),svc('environment: !override {new: explicit}'),service({'environment':{'new':'explicit'}}),'/services/web/environment','override')
case('override-scalar',svc('image: example.invalid/old:1').replace('    image: example.invalid/web:1\n',''),svc('image: !override example.invalid/new:1').replace('    image: example.invalid/web:1\n',''),{'services':{'web':{'image':'example.invalid/new:1'}}},'/services/web/image','override')
case('bind',svc('volumes: ["./data:/data:ro"]'),svc('volumes: [{type: bind, source: ./new, target: /data, read_only: false, bind: {create_host_path: true}}]'),service({'volumes':[{'type':'bind','source':'./new','target':'/data','read_only':False,'bind':{'create_host_path':True}}]}),'/services/web/volumes/0/source','replace')
case('retained-source',svc('labels: {keep: base, change: old}'),svc('labels: {change: new}'),service({'labels':{'keep':'base','change':'new'}}),'/services/web/labels/keep','add',winner=0)
case('extension-pointer','x-data: {"a/b~c": [one]}\n','x-data: {"a/b~c": [two]}\n',{'x-data':{'a/b~c':['one','two']}},'/x-data/a~1b~0c','append')
case('healthcheck-string',svc('healthcheck: {test: [CMD, echo, old]}'),svc('healthcheck: {test: echo new}'),service({'healthcheck':{'test':'echo new'}}),'/services/web/healthcheck/test','replace')
case('entrypoint-empty',svc('entrypoint: [/entry]'),svc('entrypoint: []'),service({'entrypoint':[]}),'/services/web/entrypoint','replace')
case('ipv6-long',svc('ports: [{target: 80, published: "8080", host_ip: "::1"}]'),svc('ports: [{target: 80, published: "8080", host_ip: "::1", name: ipv6}]'),service({'ports':[{**port,'host_ip':'::1','name':'ipv6'}]}),'/services/web/ports/0/name','add')
case('secret-options',svc('secrets: [{source: token, target: /token, uid: "1000", gid: "1000", mode: 0444}]')+resources,svc('secrets: [{source: newer, target: /token, uid: "1001", gid: "1001", mode: 0400}]'),{**service({'secrets':[{'source':'newer','target':'/token','uid':'1001','gid':'1001','mode':256}]}),'secrets':{'token':{'external':True},'newer':{'external':True}},'configs':{'settings':{'external':True},'newer':{'external':True}},'volumes':{'cache':{},'fresh':{}}},'/services/web/secrets/0/mode','replace')
case('volumes-append',svc('volumes: ["cache:/cache"]')+resources,svc('volumes: ["fresh:/fresh"]'),{**service({'volumes':[{'type':'volume','source':'cache','target':'/cache','read_only':False},{'type':'volume','source':'fresh','target':'/fresh','read_only':False}]}),'secrets':{'token':{'external':True},'newer':{'external':True}},'configs':{'settings':{'external':True},'newer':{'external':True}},'volumes':{'cache':{},'fresh':{}}},'/services/web/volumes/1','add')
case('resource-null-base',svc('command: [echo, base]')+'volumes: {cache: null}\n',svc('command: [echo, later]')+'volumes: {cache: {driver: local}}\n',{**service({'command':['echo','later']}),'volumes':{'cache':{'driver':'local'}}},'/volumes/cache','replace')
for c in cases:
    c['winnerLine'] = 5 if c['name'] == 'resource-null-base' else 1 if c['name'] == 'extension-pointer' else 3 if c['name'] == 'override-scalar' else 4
Path(__file__).with_name('fixtures.json').write_text(json.dumps(cases,indent=2)+'\n')
