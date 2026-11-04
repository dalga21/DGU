PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, login TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','staff')), active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS categories (id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT NOT NULL DEFAULT 'grid', assignee_id INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS locations (id INTEGER PRIMARY KEY, building TEXT NOT NULL, floor INTEGER NOT NULL, room TEXT NOT NULL, zone_name TEXT NOT NULL, qr_key TEXT UNIQUE NOT NULL, x INTEGER NOT NULL DEFAULT 0, y INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tickets (
 id INTEGER PRIMARY KEY AUTOINCREMENT, public_number TEXT UNIQUE, title TEXT NOT NULL, description TEXT NOT NULL,
 category_id TEXT NOT NULL REFERENCES categories(id), location_id INTEGER NOT NULL REFERENCES locations(id), status TEXT NOT NULL DEFAULT 'new', priority TEXT NOT NULL DEFAULT 'normal',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT, assignee_id INTEGER REFERENCES users(id),
 tracking_token_hash TEXT UNIQUE NOT NULL, request_key_hash TEXT UNIQUE NOT NULL, requester_name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', telegram_chat_id TEXT NOT NULL DEFAULT '',
 rating INTEGER CHECK(rating BETWEEN 1 AND 5), rating_comment TEXT NOT NULL DEFAULT '', ai_kind TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS tickets_filters ON tickets(status, category_id, location_id, created_at);
CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id), author_id INTEGER REFERENCES users(id), author_name TEXT NOT NULL, body TEXT NOT NULL, visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ticket_history (id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id), actor_id INTEGER REFERENCES users(id), actor_name TEXT NOT NULL, event TEXT NOT NULL, old_status TEXT, new_status TEXT, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id), storage_key TEXT UNIQUE NOT NULL, file_type TEXT NOT NULL, size INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id), channel TEXT NOT NULL, destination TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'demo', error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, sent_at TEXT);
CREATE TABLE IF NOT EXISTS ai_runs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, category_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id), action TEXT NOT NULL, created_at TEXT NOT NULL);
