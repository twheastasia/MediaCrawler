"""Platform-specific people tables with run-scoped, transactional page checkpoints."""
import hashlib
import json
from urllib.parse import urlsplit

import asyncpg
import config

PLATFORMS=('maimai','linkedin')


def dumps(value):return json.dumps(value,ensure_ascii=False,default=str)


def schema_ddl():
    sql='''CREATE TABLE IF NOT EXISTS people_acquisition_run (
 run_id text PRIMARY KEY, config jsonb NOT NULL, created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS people_acquisition_progress (
 run_id text REFERENCES people_acquisition_run(run_id), platform text NOT NULL,
 keyword text NOT NULL, state jsonb NOT NULL, updated_at timestamptz DEFAULT now(),
 PRIMARY KEY(run_id,platform,keyword));\n'''
    for p in PLATFORMS:
        sql+=f'''CREATE TABLE IF NOT EXISTS {p}_person (
 person_key text PRIMARY KEY, user_id text, name text, headline text, job_title text,
 company text, location text, school text, profile_url text, identity_kind text NOT NULL,
 raw jsonb NOT NULL, opencli_data jsonb NOT NULL,
 first_seen_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS {p}_person_source (
 run_id text REFERENCES people_acquisition_run(run_id), keyword text NOT NULL,
 page_no integer NOT NULL, position integer NOT NULL,
 person_key text REFERENCES {p}_person(person_key), raw jsonb NOT NULL,
 collected_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_id,keyword,page_no,position));
CREATE INDEX IF NOT EXISTS {p}_person_source_run_idx ON {p}_person_source(run_id,keyword,person_key);
'''
    return sql


def person(platform,row,raw):
    if platform not in PLATFORMS:raise ValueError('unsupported_people_platform')
    uid=None;url=row.get('profile_url') or raw.get('profile_url') or raw.get('home_url')
    if platform=='linkedin':
        u=urlsplit(url or '')
        if u.scheme=='https' and u.hostname in ('linkedin.com','www.linkedin.com') and u.path.startswith('/in/'):
            uid=u.path.strip('/').split('/',1)[1]
            url='https://www.linkedin.com/in/'+uid+'/'
        kind='public_profile_handle' if uid else 'unresolved_fingerprint'
    else:
        uid=next((str(raw[k]) for k in ('id','uid','user_id','mmid','encode_id','encid','ejid')
                  if raw.get(k) is not None and str(raw[k])!=''),None)
        kind='public_source_id' if uid else 'unresolved_fingerprint'
    key=uid if uid else 'unresolved:'+hashlib.sha256(dumps(raw).encode()).hexdigest()
    return {'person_key':key,'user_id':uid,'name':row.get('name') or raw.get('name'),
        'headline':row.get('headline'),'job_title':row.get('job_title') or raw.get('position'),
        'company':row.get('company') or raw.get('company'),'location':row.get('location'),
        'school':row.get('school'),'profile_url':url,'identity_kind':kind,'raw':raw,'opencli_data':row}


class PeopleStore:
    def __init__(self,conn,run_id,platform):
        if platform not in PLATFORMS:raise ValueError('unsupported_people_platform')
        self.conn,self.run_id,self.platform=conn,run_id,platform

    @classmethod
    async def open(cls,run_id,platform):
        c=config.postgres_db_config
        conn=await asyncpg.connect(host=c['host'],port=int(c['port']),user=c['user'],password=c['password'],
            database=c['db_name'],timeout=10,command_timeout=30)
        return cls(conn,run_id,platform)

    async def setup(self,settings):
        async with self.conn.transaction():
            await self.conn.execute(schema_ddl())
            await self.conn.execute('INSERT INTO people_acquisition_run(run_id,config) VALUES($1,$2::jsonb) '
                'ON CONFLICT(run_id) DO UPDATE SET config=excluded.config',self.run_id,dumps(settings))

    async def state(self,keyword,value=None):
        if value is None:
            row=await self.conn.fetchval('SELECT state FROM people_acquisition_progress WHERE run_id=$1 AND platform=$2 AND keyword=$3',
                self.run_id,self.platform,keyword)
            return json.loads(row) if row else None
        await self.conn.execute('INSERT INTO people_acquisition_progress VALUES($1,$2,$3,$4::jsonb,now()) '
            'ON CONFLICT(run_id,platform,keyword) DO UPDATE SET state=excluded.state,updated_at=now()',
            self.run_id,self.platform,keyword,dumps(value))

    async def count(self,keyword=None):
        return await self.conn.fetchval(f'''SELECT count(DISTINCT s.person_key) FROM {self.platform}_person_source s
 JOIN {self.platform}_person p USING(person_key) WHERE s.run_id=$1 AND ($2::text IS NULL OR s.keyword=$2)
 AND p.identity_kind<>'unresolved_fingerprint' ''',self.run_id,keyword)

    async def save_page(self,keyword,page_no,result,next_state):
        rows=result['rows'];raws=result.get('rawRecords') or []
        if raws and len(raws)!=len(rows):raise ValueError('people_source_row_pairing_changed')
        cols=('person_key','user_id','name','headline','job_title','company','location','school','profile_url','identity_kind')
        async with self.conn.transaction():
            for i,row in enumerate(rows):
                raw=raws[i] if raws else row
                p=person(self.platform,row,raw)
                await self.conn.execute(f'''INSERT INTO {self.platform}_person ({','.join(cols)},raw,opencli_data)
 VALUES({','.join('$'+str(n) for n in range(1,13))}::jsonb)
 ON CONFLICT(person_key) DO UPDATE SET {','.join(c+'=COALESCE(excluded.'+c+','+self.platform+'_person.'+c+')' for c in cols[1:])},
 raw=excluded.raw,opencli_data=excluded.opencli_data,updated_at=now()''',
                    *[p[k] for k in cols],dumps(raw),dumps(row))
                await self.conn.execute(f'''INSERT INTO {self.platform}_person_source
 (run_id,keyword,page_no,position,person_key,raw) VALUES($1,$2,$3,$4,$5,$6::jsonb)
 ON CONFLICT(run_id,keyword,page_no,position) DO UPDATE SET person_key=excluded.person_key,raw=excluded.raw''',
                    self.run_id,keyword,page_no,i,p['person_key'],dumps(raw))
            await self.state(keyword,next_state)

    async def close(self):await self.conn.close()
