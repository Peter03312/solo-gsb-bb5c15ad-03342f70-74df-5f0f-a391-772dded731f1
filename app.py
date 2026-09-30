#!/usr/bin/env python3
"""线下场馆失物认领系统

- 工作人员凭口令登记物品（公开描述 + 私密辨认特征 + 保管状态）
- 访客仅见公开描述，可提交辨认答案与联系方式，获得不可猜测的查询凭证
- 工作人员核对后选定唯一一份认领（并发下同一物品最多成功一次）
- 获选者凭查询凭证取得一次性交还码，工作人员核码后记录交还
- 交还码过期可重发，新码使旧码失效，重复核码不得再次交还
"""

import os
import secrets
import sqlite3
import string
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)

# ---------------------------------------------------------------- 配置

DATABASE = os.environ.get("DATABASE", os.path.join(os.path.dirname(__file__), "lostfound.db"))
STAFF_TOKEN = os.environ.get("STAFF_TOKEN", "staff-demo-token")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
CODE_TTL_MINUTES = int(os.environ.get("CODE_TTL_MINUTES", "30"))
SEED_DEMO = os.environ.get("SEED_DEMO", "0") == "1"
PORT = int(os.environ.get("PORT", "8000"))

# 物品保管状态
ITEM_OPEN = "open"          # 在库保管中，接受认领申请
ITEM_MATCHED = "matched"    # 已选定认领人，等待核码交还
ITEM_RETURNED = "returned"  # 已交还
ITEM_REMOVED = "removed"    # 已撤下（不再接受认领）

ITEM_STATUS_LABEL = {
    ITEM_OPEN: "保管中",
    ITEM_MATCHED: "待交还",
    ITEM_RETURNED: "已交还",
    ITEM_REMOVED: "已撤下",
}

# 认领申请状态
CLAIM_PENDING = "pending"
CLAIM_SELECTED = "selected"
CLAIM_NOT_SELECTED = "not_selected"

CLAIM_STATUS_LABEL = {
    CLAIM_PENDING: "核对中",
    CLAIM_SELECTED: "已获选",
    CLAIM_NOT_SELECTED: "未获选",
}

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混淆字符

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


# ---------------------------------------------------------------- 数据库

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    public_desc      TEXT NOT NULL,
    private_features TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open','matched','returned','removed')),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES items(id),
    token      TEXT NOT NULL UNIQUE,          -- 访客查询凭证（不可猜测）
    answer     TEXT NOT NULL,                 -- 辨认答案（私密，仅工作人员可见）
    contact    TEXT NOT NULL,                 -- 联系方式（私密，仅工作人员可见）
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending','selected','not_selected')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_item ON claims(item_id);

CREATE TABLE IF NOT EXISTS return_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id    INTEGER NOT NULL REFERENCES claims(id),
    code        TEXT NOT NULL UNIQUE,         -- 一次性交还码
    expires_at  TEXT NOT NULL,
    used_at     TEXT,                         -- 已核销时间，NULL 表示未使用
    superseded  INTEGER NOT NULL DEFAULT 0,   -- 1 表示已被新码取代
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_return_codes_claim ON return_codes(claim_id);
"""


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(os.path.abspath(DATABASE)), exist_ok=True)
        conn = sqlite3.connect(DATABASE, isolation_level=None)  # 手动控制事务
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DATABASE)), exist_ok=True)
    conn = sqlite3.connect(DATABASE)
    try:
        conn.executescript(SCHEMA)
        if SEED_DEMO:
            cur = conn.execute("SELECT COUNT(*) FROM items")
            if cur.fetchone()[0] == 0:
                now = iso(utcnow())
                conn.execute(
                    "INSERT INTO items (public_desc, private_features, status, created_at, updated_at)"
                    " VALUES (?,?,?,?,?)",
                    ("黑色双肩包一只（演示数据）",
                     "包内侧袋有一张尾号 4172 的电影票；拉链头用红绳系着",
                     ITEM_OPEN, now, now))
                conn.execute(
                    "INSERT INTO items (public_desc, private_features, status, created_at, updated_at)"
                    " VALUES (?,?,?,?,?)",
                    ("银色保温杯一个（演示数据）",
                     "杯底贴有手写字母 'L.W.' 的蓝色标签",
                     ITEM_OPEN, now, now))
                conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 工具

def new_claim_token():
    return secrets.token_urlsafe(32)


def new_return_code():
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def staff_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("staff"):
            return redirect(url_for("staff_login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def get_item_or_404(db, item_id):
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def valid_code_for_claim(db, claim_id):
    """返回该认领当前有效（未用、未过期、未被取代）的交还码。"""
    return db.execute(
        "SELECT * FROM return_codes WHERE claim_id = ? AND used_at IS NULL"
        " AND superseded = 0 AND expires_at > ?",
        (claim_id, iso(utcnow()))).fetchone()


def issue_code(db, claim_id):
    """签发新交还码：作废旧码，生成新码。"""
    db.execute("UPDATE return_codes SET superseded = 1 WHERE claim_id = ? AND used_at IS NULL",
               (claim_id,))
    code = new_return_code()
    expires = iso(utcnow() + timedelta(minutes=CODE_TTL_MINUTES))
    db.execute(
        "INSERT INTO return_codes (claim_id, code, expires_at, used_at, superseded, created_at)"
        " VALUES (?,?,?,NULL,0,?)",
        (claim_id, code, expires, iso(utcnow())))
    return code, expires


# ---------------------------------------------------------------- 访客页面

@app.route("/")
def index():
    db = get_db()
    items = db.execute(
        "SELECT id, public_desc, status, created_at FROM items ORDER BY id DESC").fetchall()
    return render_template("index.html", items=items, status_label=ITEM_STATUS_LABEL)


@app.route("/items/<int:item_id>")
def item_detail(item_id):
    db = get_db()
    item = get_item_or_404(db, item_id)
    return render_template("item.html", item=item, status_label=ITEM_STATUS_LABEL)


@app.route("/items/<int:item_id>/claim", methods=["POST"])
def submit_claim(item_id):
    db = get_db()
    answer = (request.form.get("answer") or "").strip()
    contact = (request.form.get("contact") or "").strip()
    if not answer or not contact:
        flash("请填写辨认答案和联系方式。", "error")
        return redirect(url_for("item_detail", item_id=item_id))
    if len(answer) > 2000 or len(contact) > 200:
        flash("填写内容过长。", "error")
        return redirect(url_for("item_detail", item_id=item_id))

    token = new_claim_token()
    now = iso(utcnow())
    # 仅“保管中”的物品接受新申请；已撤下/已交还/已选定的一律拒绝。
    # 状态检查与插入放在同一写事务内，避免与工作人员操作交错。
    db.execute("BEGIN IMMEDIATE")
    try:
        item = db.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            db.execute("ROLLBACK")
            abort(404)
        if item["status"] != ITEM_OPEN:
            db.execute("ROLLBACK")
            flash("该物品当前不接受新的认领申请（已撤下、已选定或已交还）。", "error")
            return redirect(url_for("item_detail", item_id=item_id))
        db.execute(
            "INSERT INTO claims (item_id, token, answer, contact, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, token, answer, contact, CLAIM_PENDING, now))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return redirect(url_for("claim_created", token=token))


@app.route("/claim/<token>/created")
def claim_created(token):
    db = get_db()
    claim = db.execute("SELECT * FROM claims WHERE token = ?", (token,)).fetchone()
    if claim is None:
        abort(404)
    return render_template("claim_created.html", token=token)


@app.route("/claim/<token>")
def claim_status(token):
    db = get_db()
    claim = db.execute(
        "SELECT c.*, i.public_desc, i.status AS item_status FROM claims c"
        " JOIN items i ON i.id = c.item_id WHERE c.token = ?", (token,)).fetchone()
    if claim is None:
        abort(404)

    code_row = None
    if claim["status"] == CLAIM_SELECTED and claim["item_status"] == ITEM_MATCHED:
        code_row = valid_code_for_claim(db, claim["id"])
        if code_row is None:
            # 首次查看或旧码已过期：自动签发新码（新码使旧码失效）
            db.execute("BEGIN IMMEDIATE")
            try:
                code_row = valid_code_for_claim(db, claim["id"])
                if code_row is None:
                    code, expires = issue_code(db, claim["id"])
                    code_row = {"code": code, "expires_at": expires}
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    return render_template("claim_status.html",
                           claim=claim, code=code_row,
                           claim_label=CLAIM_STATUS_LABEL,
                           item_label=ITEM_STATUS_LABEL,
                           ttl=CODE_TTL_MINUTES)


@app.route("/claim/<token>/reissue", methods=["POST"])
def reissue_code(token):
    db = get_db()
    claim = db.execute(
        "SELECT c.*, i.status AS item_status FROM claims c"
        " JOIN items i ON i.id = c.item_id WHERE c.token = ?", (token,)).fetchone()
    if claim is None:
        abort(404)
    if claim["status"] != CLAIM_SELECTED or claim["item_status"] != ITEM_MATCHED:
        flash("当前状态无法重发交还码。", "error")
        return redirect(url_for("claim_status", token=token))
    db.execute("BEGIN IMMEDIATE")
    try:
        issue_code(db, claim["id"])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    flash("已签发新交还码，旧码立即失效。", "ok")
    return redirect(url_for("claim_status", token=token))


# ---------------------------------------------------------------- 工作人员

@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("token") or "", STAFF_TOKEN):
            session["staff"] = True
            return redirect(request.args.get("next") or url_for("staff_items"))
        flash("口令错误。", "error")
    return render_template("staff_login.html")


@app.route("/staff/logout", methods=["POST"])
def staff_logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/staff")
@staff_required
def staff_items():
    db = get_db()
    items = db.execute(
        "SELECT i.*, "
        " (SELECT COUNT(*) FROM claims c WHERE c.item_id = i.id AND c.status = 'pending') AS pending_count,"
        " (SELECT COUNT(*) FROM claims c WHERE c.item_id = i.id) AS claim_count"
        " FROM items i ORDER BY i.id DESC").fetchall()
    return render_template("staff_items.html", items=items, status_label=ITEM_STATUS_LABEL)


@app.route("/staff/items/new", methods=["GET", "POST"])
@staff_required
def staff_item_new():
    if request.method == "POST":
        public_desc = (request.form.get("public_desc") or "").strip()
        private_features = (request.form.get("private_features") or "").strip()
        if not public_desc or not private_features:
            flash("公开描述与私密辨认特征均为必填。", "error")
            return render_template("staff_item_new.html")
        now = iso(utcnow())
        db = get_db()
        cur = db.execute(
            "INSERT INTO items (public_desc, private_features, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?)",
            (public_desc, private_features, ITEM_OPEN, now, now))
        flash(f"物品 #{cur.lastrowid} 登记成功。", "ok")
        return redirect(url_for("staff_item_detail", item_id=cur.lastrowid))
    return render_template("staff_item_new.html")


@app.route("/staff/items/<int:item_id>")
@staff_required
def staff_item_detail(item_id):
    db = get_db()
    item = get_item_or_404(db, item_id)
    claims = db.execute(
        "SELECT * FROM claims WHERE item_id = ? ORDER BY id", (item_id,)).fetchall()
    return render_template("staff_item_detail.html", item=item, claims=claims,
                           status_label=ITEM_STATUS_LABEL, claim_label=CLAIM_STATUS_LABEL)


@app.route("/staff/items/<int:item_id>/select/<int:claim_id>", methods=["POST"])
@staff_required
def staff_select_claim(item_id, claim_id):
    """选定一份认领。并发下同一物品最多成功一次：
    写事务内复查物品状态，且 UPDATE 带状态条件，第二个并发请求必然失败。"""
    db = get_db()
    now = iso(utcnow())
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (ITEM_MATCHED, now, item_id, ITEM_OPEN))
        if cur.rowcount != 1:
            db.execute("ROLLBACK")
            flash("选定失败：该物品已不在可认领状态（可能已被其他操作选定/撤下/交还）。", "error")
            return redirect(url_for("staff_item_detail", item_id=item_id))
        cur = db.execute(
            "UPDATE claims SET status = ? WHERE id = ? AND item_id = ? AND status = ?",
            (CLAIM_SELECTED, claim_id, item_id, CLAIM_PENDING))
        if cur.rowcount != 1:
            db.execute("ROLLBACK")
            flash("选定失败：该认领申请不存在或已被处理。", "error")
            return redirect(url_for("staff_item_detail", item_id=item_id))
        db.execute(
            "UPDATE claims SET status = ? WHERE item_id = ? AND status = ?",
            (CLAIM_NOT_SELECTED, item_id, CLAIM_PENDING))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    flash(f"已选定认领 #{claim_id}，其余申请标记为未获选。", "ok")
    return redirect(url_for("staff_item_detail", item_id=item_id))


@app.route("/staff/items/<int:item_id>/remove", methods=["POST"])
@staff_required
def staff_remove_item(item_id):
    """撤下物品：此后拒绝新申请。已交还的物品不可再变更。"""
    db = get_db()
    now = iso(utcnow())
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE id = ? AND status IN (?, ?)",
            (ITEM_REMOVED, now, item_id, ITEM_OPEN, ITEM_MATCHED))
        if cur.rowcount != 1:
            db.execute("ROLLBACK")
            flash("撤下失败：物品不存在或已交还。", "error")
            return redirect(url_for("staff_item_detail", item_id=item_id))
        db.execute(
            "UPDATE claims SET status = ? WHERE item_id = ? AND status = ?",
            (CLAIM_NOT_SELECTED, item_id, CLAIM_PENDING))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    flash("物品已撤下，不再接受新的认领申请。", "ok")
    return redirect(url_for("staff_item_detail", item_id=item_id))


@app.route("/staff/verify", methods=["GET", "POST"])
@staff_required
def staff_verify():
    result = None
    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        db = get_db()
        now = iso(utcnow())
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT rc.*, c.item_id FROM return_codes rc"
                " JOIN claims c ON c.id = rc.claim_id WHERE rc.code = ?", (code,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                result = ("error", "交还码不存在。")
            elif row["used_at"] is not None:
                db.execute("ROLLBACK")
                result = ("error", f"该交还码已于 {row['used_at']} (UTC) 核销，不能重复交还。")
            elif row["superseded"]:
                db.execute("ROLLBACK")
                result = ("error", "该交还码已被新码取代，无效。")
            elif row["expires_at"] <= now:
                db.execute("ROLLBACK")
                result = ("error", "该交还码已过期，请让认领人重发。")
            else:
                # 核销码 + 物品置为已交还，均带条件更新，重复核码不会再次生效
                cur = db.execute(
                    "UPDATE return_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
                    (now, row["id"]))
                cur2 = db.execute(
                    "UPDATE items SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (ITEM_RETURNED, now, row["item_id"], ITEM_MATCHED))
                if cur.rowcount == 1 and cur2.rowcount == 1:
                    db.execute("COMMIT")
                    result = ("ok", f"核销成功：物品 #{row['item_id']} 已记录交还（{now} UTC）。")
                else:
                    db.execute("ROLLBACK")
                    result = ("error", "核销失败：该码已被使用或物品状态已变化。")
        except Exception:
            db.execute("ROLLBACK")
            raise
    return render_template("staff_verify.html", result=result)


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", message="页面不存在或凭证无效。"), 404


# ---------------------------------------------------------------- 入口

init_db()

if __name__ == "__main__":
    if STAFF_TOKEN == "staff-demo-token":
        print("警告：正在使用默认工作人员口令，请通过环境变量 STAFF_TOKEN 配置。", file=sys.stderr)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
