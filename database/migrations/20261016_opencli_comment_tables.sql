
CREATE TABLE IF NOT EXISTS maimai_comment (
	id SERIAL NOT NULL, 
	content_id TEXT, 
	comment_id TEXT, 
	user_id TEXT, 
	content TEXT, 
	parent_comment_id TEXT, 
	add_ts BIGINT, 
	last_modify_ts BIGINT, 
	PRIMARY KEY (id)
)

;

CREATE TABLE IF NOT EXISTS linkedin_comment (
	id SERIAL NOT NULL, 
	content_id TEXT, 
	comment_id TEXT, 
	user_id TEXT, 
	content TEXT, 
	parent_comment_id TEXT, 
	add_ts BIGINT, 
	last_modify_ts BIGINT, 
	PRIMARY KEY (id)
)

;
ALTER TABLE maimai_comment ADD COLUMN IF NOT EXISTS user_id text;
COMMENT ON COLUMN maimai_comment.user_id IS '评论用户的公开平台ID；缺失时为NULL，不以匿名哈希代替';
CREATE UNIQUE INDEX IF NOT EXISTS maimai_comment_content_comment_uidx ON maimai_comment (content_id, comment_id);
ALTER TABLE linkedin_comment ADD COLUMN IF NOT EXISTS user_id text;
COMMENT ON COLUMN linkedin_comment.user_id IS '评论用户的公开平台ID；缺失时为NULL，不以匿名哈希代替';
CREATE UNIQUE INDEX IF NOT EXISTS linkedin_comment_content_comment_uidx ON linkedin_comment (content_id, comment_id);
