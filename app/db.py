"""Database layer for multi-tenant Content Engine."""

import os
import json
import sqlite3
import hashlib
import secrets
from contextlib import contextmanager

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'engine.db'))


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'client',
            invite_code TEXT,
            onboarded INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            avatar_context TEXT NOT NULL DEFAULT '',
            voice_context TEXT NOT NULL DEFAULT '',
            calibration TEXT NOT NULL DEFAULT '',
            algorithm_context TEXT NOT NULL DEFAULT '',
            trade_chapters TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')


def hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f'{salt}:{h}'


def check_password(stored, password):
    salt, h = stored.split(':')
    return hashlib.sha256((salt + password).encode()).hexdigest() == h


def create_invite(email, name, role='client'):
    code = secrets.token_urlsafe(16)
    with get_db() as db:
        db.execute(
            'INSERT INTO users (email, name, role, invite_code) VALUES (?, ?, ?, ?)',
            (email.lower().strip(), name, role, code)
        )
    return code


def accept_invite(code, password):
    with get_db() as db:
        user = db.execute('SELECT * FROM users WHERE invite_code = ?', (code,)).fetchone()
        if not user:
            return None
        db.execute(
            'UPDATE users SET password_hash = ?, invite_code = NULL WHERE id = ?',
            (hash_password(password), user['id'])
        )
        db.execute(
            'INSERT OR IGNORE INTO profiles (user_id) VALUES (?)',
            (user['id'],)
        )
        return create_session(db, user['id'])


def login(email, password):
    with get_db() as db:
        user = db.execute('SELECT * FROM users WHERE email = ?', (email.lower().strip(),)).fetchone()
        if not user or not user['password_hash']:
            return None
        if not check_password(user['password_hash'], password):
            return None
        return create_session(db, user['id'])


def create_session(db, user_id):
    token = secrets.token_urlsafe(32)
    db.execute('INSERT INTO sessions (token, user_id) VALUES (?, ?)', (token, user_id))
    return token


def get_user_by_token(token):
    if not token:
        return None
    with get_db() as db:
        row = db.execute('''
            SELECT u.*, p.avatar_context, p.voice_context, p.calibration,
                   p.algorithm_context, p.trade_chapters
            FROM sessions s
            JOIN users u ON s.user_id = u.id
            LEFT JOIN profiles p ON p.user_id = u.id
            WHERE s.token = ?
        ''', (token,)).fetchone()
        if not row:
            return None
        return dict(row)


def get_profile(user_id):
    with get_db() as db:
        row = db.execute('SELECT * FROM profiles WHERE user_id = ?', (user_id,)).fetchone()
        return dict(row) if row else None


def save_profile(user_id, avatar_context='', voice_context='', calibration='', algorithm_context=''):
    with get_db() as db:
        db.execute('''INSERT INTO profiles (user_id, avatar_context, voice_context, calibration, algorithm_context, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                avatar_context = excluded.avatar_context,
                voice_context = excluded.voice_context,
                calibration = excluded.calibration,
                algorithm_context = excluded.algorithm_context,
                updated_at = CURRENT_TIMESTAMP
        ''', (user_id, avatar_context, voice_context, calibration, algorithm_context))


def mark_onboarded(user_id):
    with get_db() as db:
        db.execute('UPDATE users SET onboarded = 1 WHERE id = ?', (user_id,))


def list_clients():
    with get_db() as db:
        rows = db.execute(
            'SELECT id, email, name, role, onboarded, created_at FROM users ORDER BY created_at DESC'
        ).fetchall()
        return [dict(r) for r in rows]


def logout(token):
    with get_db() as db:
        db.execute('DELETE FROM sessions WHERE token = ?', (token,))


init_db()
