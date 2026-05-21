-- ============================================================
--  NEXUS AUTH  –  MySQL Schema
-- ============================================================

CREATE DATABASE IF NOT EXISTS nexus_auth CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE nexus_auth;

-- Users table
CREATE TABLE IF NOT EXISTS users (
    id            INT UNSIGNED    AUTO_INCREMENT PRIMARY KEY,
    full_name     VARCHAR(120)    NOT NULL,
    username      VARCHAR(60)     NOT NULL UNIQUE,
    email         VARCHAR(180)    NOT NULL UNIQUE,
    phone         VARCHAR(20)     DEFAULT NULL,
    password_hash VARCHAR(255)    NOT NULL,
    avatar_color  VARCHAR(7)      NOT NULL DEFAULT '#7F77DD',
    role          ENUM('user','admin') NOT NULL DEFAULT 'user',
    is_verified   BOOLEAN         NOT NULL DEFAULT FALSE,
    is_locked     BOOLEAN         NOT NULL DEFAULT FALSE,
    failed_attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
    last_login    DATETIME        DEFAULT NULL,
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_email    (email),
    INDEX idx_username (username)
) ENGINE=InnoDB;

-- Password reset tokens
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id         INT UNSIGNED  AUTO_INCREMENT PRIMARY KEY,
    user_id    INT UNSIGNED  NOT NULL,
    token      VARCHAR(100)  NOT NULL UNIQUE,
    expires_at DATETIME      NOT NULL,
    used       BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Active sessions
CREATE TABLE IF NOT EXISTS sessions (
    id         INT UNSIGNED  AUTO_INCREMENT PRIMARY KEY,
    user_id    INT UNSIGNED  NOT NULL,
    token      VARCHAR(255)  NOT NULL UNIQUE,
    ip_address VARCHAR(45)   DEFAULT NULL,
    user_agent TEXT          DEFAULT NULL,
    expires_at DATETIME      NOT NULL,
    created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Login audit log
CREATE TABLE IF NOT EXISTS login_logs (
    id         INT UNSIGNED  AUTO_INCREMENT PRIMARY KEY,
    user_id    INT UNSIGNED  DEFAULT NULL,
    email      VARCHAR(180)  NOT NULL,
    ip_address VARCHAR(45)   DEFAULT NULL,
    success    BOOLEAN       NOT NULL DEFAULT FALSE,
    reason     VARCHAR(120)  DEFAULT NULL,
    created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_user_id (user_id)
) ENGINE=InnoDB;

-- Demo seed account  (password: Admin@123)
INSERT IGNORE INTO users (full_name, username, email, password_hash, role, is_verified, avatar_color)
VALUES (
    'Nexus Admin',
    'admin',
    'admin@nexus.dev',
    '$2b$12$placeholder_replace_with_real_hash',
    'admin',
    TRUE,
    '#1D9E75'
);
