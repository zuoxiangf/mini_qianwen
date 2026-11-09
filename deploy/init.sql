-- mini_qianwen 会话/用户存储建表脚本
-- 由 mysql 容器首次启动时自动执行（挂载到 /docker-entrypoint-initdb.d/）
-- 注意：sessions.sid 必须为主键，db.py 的 ON DUPLICATE KEY UPDATE 依赖它

-- 用户表：username 唯一，pw_hash 存 bcrypt 哈希（绝不明文存储）
CREATE TABLE IF NOT EXISTS users (
  uid       INT AUTO_INCREMENT PRIMARY KEY,
  username  VARCHAR(64)  NOT NULL UNIQUE,
  pw_hash   VARCHAR(128) NOT NULL,           -- bcrypt 串固定 60 字节，留余量
  created   DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS messages (
  id       BIGINT AUTO_INCREMENT PRIMARY KEY,
  sid      VARCHAR(36) NOT NULL,
  role     VARCHAR(16) NOT NULL,
  content  MEDIUMTEXT  NOT NULL,          -- 用 MEDIUMTEXT 避免长回答超 64KB 被截断
  created  DATETIME    DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sid_id (sid, id)                 -- 覆盖 WHERE sid=? ORDER BY id DESC LIMIT n
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sessions (
  sid         VARCHAR(36) PRIMARY KEY,
  uid         INT NULL,                    -- 会话归属用户（鉴权后写入，旧数据可为 NULL）
  last_active DATETIME,
  KEY idx_last_active (last_active),
  KEY idx_uid_active (uid, last_active)    -- 覆盖“按用户列会话 + 活跃时间倒序”
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 若为已有数据库升级（init.sql 仅对新装生效），手动执行一次：
--   ALTER TABLE sessions ADD COLUMN uid INT NULL AFTER sid;
--   ALTER TABLE sessions ADD KEY idx_uid_active (uid, last_active);
