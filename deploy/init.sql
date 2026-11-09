-- mini_qianwen 会话存储建表脚本
-- 由 mysql 容器首次启动时自动执行（挂载到 /docker-entrypoint-initdb.d/）
-- 注意：sessions.sid 必须为主键，db.py 的 ON DUPLICATE KEY UPDATE 依赖它

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
  last_active DATETIME,
  KEY idx_last_active (last_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
