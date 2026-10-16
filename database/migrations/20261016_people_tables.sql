CREATE TABLE IF NOT EXISTS people_acquisition_run (
 run_id text PRIMARY KEY, config jsonb NOT NULL, created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS people_acquisition_progress (
 run_id text REFERENCES people_acquisition_run(run_id), platform text NOT NULL,
 keyword text NOT NULL, state jsonb NOT NULL, updated_at timestamptz DEFAULT now(),
 PRIMARY KEY(run_id,platform,keyword));
CREATE TABLE IF NOT EXISTS maimai_person (
 person_key text PRIMARY KEY, user_id text, name text, headline text, job_title text,
 company text, location text, school text, profile_url text, identity_kind text NOT NULL,
 raw jsonb NOT NULL, opencli_data jsonb NOT NULL,
 first_seen_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS maimai_person_source (
 run_id text REFERENCES people_acquisition_run(run_id), keyword text NOT NULL,
 page_no integer NOT NULL, position integer NOT NULL,
 person_key text REFERENCES maimai_person(person_key), raw jsonb NOT NULL,
 collected_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_id,keyword,page_no,position));
CREATE INDEX IF NOT EXISTS maimai_person_source_run_idx ON maimai_person_source(run_id,keyword,person_key);
CREATE TABLE IF NOT EXISTS linkedin_person (
 person_key text PRIMARY KEY, user_id text, name text, headline text, job_title text,
 company text, location text, school text, profile_url text, identity_kind text NOT NULL,
 raw jsonb NOT NULL, opencli_data jsonb NOT NULL,
 first_seen_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS linkedin_person_source (
 run_id text REFERENCES people_acquisition_run(run_id), keyword text NOT NULL,
 page_no integer NOT NULL, position integer NOT NULL,
 person_key text REFERENCES linkedin_person(person_key), raw jsonb NOT NULL,
 collected_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_id,keyword,page_no,position));
CREATE INDEX IF NOT EXISTS linkedin_person_source_run_idx ON linkedin_person_source(run_id,keyword,person_key);
