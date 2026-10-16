"""Dedicated PostgreSQL schema for public WeChat article acquisition."""

DDL = """
CREATE TABLE IF NOT EXISTS wxarticle_run (
 run_id text PRIMARY KEY, config jsonb NOT NULL, created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS wxarticle_content (
 platform text NOT NULL, content_type text NOT NULL, content_id text NOT NULL,
 author_id text, title text, body text, source_url text, published_at bigint,
 raw jsonb NOT NULL, updated_at timestamptz DEFAULT now(),
 PRIMARY KEY(platform,content_type,content_id));
CREATE TABLE IF NOT EXISTS wxarticle_author (
 platform text NOT NULL, author_id text NOT NULL, name text, home_url text,
 raw jsonb NOT NULL, updated_at timestamptz DEFAULT now(), PRIMARY KEY(platform,author_id));
CREATE TABLE IF NOT EXISTS wxarticle_link (
 run_id text REFERENCES wxarticle_run(run_id), platform text, keyword text,
 role text CHECK(role IN ('search','history')), content_type text, content_id text,
 PRIMARY KEY(run_id,platform,keyword,role,content_type,content_id),
 FOREIGN KEY(platform,content_type,content_id)
 REFERENCES wxarticle_content(platform,content_type,content_id));
CREATE TABLE IF NOT EXISTS wxarticle_media (
 platform text, content_type text, content_id text, stem text,
 source_url text, local_path text, bytes bigint, status text, error text,
 updated_at timestamptz DEFAULT now(), PRIMARY KEY(platform,content_type,content_id,stem),
 FOREIGN KEY(platform,content_type,content_id)
 REFERENCES wxarticle_content(platform,content_type,content_id));
CREATE TABLE IF NOT EXISTS wxarticle_progress (
 run_id text REFERENCES wxarticle_run(run_id), platform text, task_key text,
 state jsonb NOT NULL, updated_at timestamptz DEFAULT now(),
 PRIMARY KEY(run_id,platform,task_key));
CREATE INDEX IF NOT EXISTS wxarticle_content_author_idx
 ON wxarticle_content(platform,author_id,published_at DESC);

CREATE TABLE IF NOT EXISTS wxarticle_task (
 run_id text NOT NULL, task_key text NOT NULL, kind text NOT NULL,
 payload jsonb NOT NULL, status text NOT NULL DEFAULT 'pending',
 attempts integer NOT NULL DEFAULT 0, error_code text,
 updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(run_id,task_key));
CREATE TABLE IF NOT EXISTS wxarticle_source (
 run_id text NOT NULL, source_key text NOT NULL, candidate_key text NOT NULL,
 content_id text, evidence jsonb NOT NULL,
 PRIMARY KEY(run_id,source_key,candidate_key));
CREATE TABLE IF NOT EXISTS wxarticle_alias (
 alias_key text PRIMARY KEY, content_id text NOT NULL);
CREATE TABLE IF NOT EXISTS wxarticle_media_detail (
 content_id text NOT NULL, stem text NOT NULL, media_type text NOT NULL,
 sha256 text, status text NOT NULL, error_code text,
 updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(content_id,stem));
CREATE INDEX IF NOT EXISTS wxarticle_task_status_idx ON wxarticle_task(run_id,kind,status);
CREATE INDEX IF NOT EXISTS wxarticle_source_candidate_idx ON wxarticle_source(run_id,candidate_key);
"""

from collection.comment_store import comment_ddl
DDL += comment_ddl('wxarticle')
